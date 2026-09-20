"""只读枚举微信浏览器标签，不激活窗口、不截图、不切换标签。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def has_search_tab(hwnd: int) -> bool:
    """隔离 UIA 调用并设置超时，防止失去响应的窗口卡住控制台轮询。"""
    try:
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), str(hwnd)],
            capture_output=True, text=True, encoding="utf-8", timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if result.returncode:
            return False
        return any("搜一搜" in name for name in json.loads(result.stdout))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False


def _read_tab_names(hwnd: int) -> list[str]:
    # 仅在独立探测进程内加载 COM，避免 HTTP 工作线程之间共享接口对象。
    from comtypes.client import CreateObject, GetModule

    uia = GetModule("UIAutomationCore.dll")
    automation = CreateObject(uia.CUIAutomation, interface=uia.IUIAutomation)
    root = automation.ElementFromHandle(hwnd)
    # 查询范围限定为目标 HWND 的标签控件，不读取正文或其他应用。
    condition = automation.CreatePropertyCondition(30003, 50019)
    tabs = root.FindAll(4, condition)  # TreeScope_Descendants
    return [tabs.GetElement(index).CurrentName or "" for index in range(tabs.Length)]


def probe_page(hwnd: int) -> dict:
    """读取页面证据；探测失败与未发现页面分开记录。"""
    try:
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), str(hwnd), "--page"],
            capture_output=True, text=True, encoding="utf-8", timeout=8,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if result.returncode:
            return {"ok": False, "error": result.stderr[-500:]}
        return json.loads(result.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": str(exc)}


def select_tab(hwnd: int, identity: tuple) -> bool:
    """按新枚举的 RuntimeId 选择标签，禁止使用旧元素索引或标题猜测。"""
    try:
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), str(hwnd), "--select-tab", json.dumps(identity)],
            capture_output=True, text=True, encoding="utf-8", timeout=8,
            creationflags=subprocess.CREATE_NO_WINDOW)
        return result.returncode == 0 and json.loads(result.stdout) is True
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False


def _select_tab(hwnd: int, identity: list) -> bool:
    from comtypes.client import CreateObject, GetModule
    uia = GetModule("UIAutomationCore.dll")
    automation = CreateObject(uia.CUIAutomation, interface=uia.IUIAutomation)
    root = automation.ElementFromHandle(hwnd)
    tabs = root.FindAll(4, automation.CreatePropertyCondition(30003, 50019))
    matches = [tabs.GetElement(i) for i in range(tabs.Length)
               if list(tabs.GetElement(i).GetRuntimeId()) == identity]
    if len(matches) != 1:
        return False
    matches[0].GetCurrentPattern(10010).QueryInterface(uia.IUIAutomationSelectionItemPattern).Select()
    return True


def _read_page(hwnd: int) -> dict:
    import ctypes
    from comtypes.client import CreateObject, GetModule

    # 返回物理屏幕坐标，与采集器 DPI 感知的点击坐标保持一致。
    ctypes.windll.user32.SetProcessDPIAware()
    uia = GetModule("UIAutomationCore.dll")
    automation = CreateObject(uia.CUIAutomation, interface=uia.IUIAutomation)
    root = automation.ElementFromHandle(hwnd)
    documents = root.FindAll(4, automation.CreatePropertyCondition(30003, 50030))
    payload = {"ok": True, "hwnd": hwnd, "documents": [], "tabs": []}
    tabs = root.FindAll(4, automation.CreatePropertyCondition(30003, 50019))
    for index in range(tabs.Length):
        tab = tabs.GetElement(index)
        try:
            selected = bool(tab.GetCurrentPropertyValue(30079))
        except Exception:
            selected = None  # 不支持选择状态时回退完整遍历。
        payload["tabs"].append({"id": list(tab.GetRuntimeId()), "name": tab.CurrentName,
                                "selected": selected})
    if not payload["tabs"]:
        # 微信部分版本将标签暴露为 Pane/Tab，没有标准选择状态。
        # 单独记录结构供适配验证，不伪造 selected 或以“关闭按钮可见”猜测选中项。
        payload["custom_tabs"] = []
        strips = root.FindAll(4, automation.CreatePropertyCondition(30012, "TabStrip"))
        for strip_index in range(strips.Length):
            custom = strips.GetElement(strip_index).FindAll(
                4, automation.CreatePropertyCondition(30012, "Tab"))
            for tab_index in range(custom.Length):
                tab = custom.GetElement(tab_index)
                rect = tab.CurrentBoundingRectangle
                payload["custom_tabs"].append({
                    "id": list(tab.GetRuntimeId()), "name": tab.CurrentName or "",
                    "rect": [rect.left, rect.top, rect.right, rect.bottom],
                    "offscreen": bool(tab.CurrentIsOffscreen),
                })
        payload["tab_probe_mode"] = "custom-unverified" if payload["custom_tabs"] else "unavailable"
    else:
        payload["tab_probe_mode"] = "standard"
    for index in range(documents.Length):
        doc = documents.GetElement(index)
        if doc.CurrentIsOffscreen:
            continue
        elements = doc.FindAll(4, automation.CreateTrueCondition())
        rows = []
        for row_index in range(min(elements.Length, 1500)):
            element = elements.GetElement(row_index)
            name = element.CurrentName or ""
            if not name or element.CurrentControlType not in {50000, 50005, 50020}:
                continue
            rect = element.CurrentBoundingRectangle
            rows.append({"text": name, "type": element.CurrentControlType,
                         "rect": [rect.left, rect.top, rect.right, rect.bottom],
                         "offscreen": bool(element.CurrentIsOffscreen)})
        payload["documents"].append({"id": list(doc.GetRuntimeId()),
                                      "name": doc.CurrentName or "", "rows": rows})
    return payload


if __name__ == "__main__":
    if "--select-tab" in sys.argv:
        value = _select_tab(int(sys.argv[1]), json.loads(sys.argv[3]))
    else:
        value = _read_page(int(sys.argv[1])) if "--page" in sys.argv else _read_tab_names(int(sys.argv[1]))
    print(json.dumps(value, ensure_ascii=True))

