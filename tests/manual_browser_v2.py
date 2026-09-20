"""手动实机回归：不写 MongoDB、不发送通知，只验证文章链接和页面往返。"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import wechat_visual_rpa as rpa


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True)
    parser.add_argument("--articles", type=int, default=2)
    parser.add_argument("--open-profile", action="store_true")
    args = parser.parse_args()
    output = rpa.RPA_DIR / "output" / "browser-v2-validation" / rpa.safe_path_name(args.account)
    output.mkdir(parents=True, exist_ok=True)
    if args.open_profile:
        window, _ = rpa.search_and_open_profile(args.account, output / "search", allow_vl=False)
    else:
        window = rpa.find_navigation_browser()
        rpa.activate_window(window.hwnd)
        navigator = rpa.browser_navigator(window, args.account)
        for _ in range(12):
            if navigator.observe().role == "profile":
                break
            navigator.next_tab()
        else:
            raise RuntimeError("请先打开目标公众号主页")
    navigator = rpa.browser_navigator(window, args.account)
    initial = navigator.inventory()
    preserved = {p.key for p in initial}
    existing_titles = {p.name for p in initial if p.role == "article"}
    records = []
    rpa.press_ctrl_home()
    for index in range(args.articles):
        before = navigator.checkpoint()
        feed = rpa.profile_feed_from_uia(window, args.account)
        candidates = [card for card in feed["articles"] if card["title"] not in existing_titles]
        if not candidates:
            raise RuntimeError("视口内没有可验证的新文章")
        card = candidates[0]
        print(json.dumps({"stage": "open_article", "title": card["title"]}, ensure_ascii=False), flush=True)
        rpa.click(*card["screen_point"])
        rpa.time.sleep(2.5)
        try:
            page = rpa.wait_for_article_page(window, args.account, card["title"])
            url = rpa.copy_article_url(window.hwnd, window.rect, output, allow_vl=False)
            parsed = rpa.parse_page(url)
            if parsed["account_name"] != args.account or not rpa.titles_match(card["title"], parsed["title"]):
                raise RuntimeError("文章链接与目标账号/标题不一致")
            records.append({"title": parsed["title"], "url": url, "account": parsed["account_name"]})
        finally:
            method = navigator.restore(before)
        existing_titles.add(card["title"])
        records[-1]["return_method"] = method
        print(json.dumps(records[-1], ensure_ascii=False), flush=True)
    final = navigator.inventory()
    if not preserved <= {p.key for p in final}:
        raise RuntimeError("原有页面未全部保留")
    result = {"passed": True, "records": records, "original_pages_preserved": True}
    (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()

