"""用行间距、字号及对齐关系恢复多行标题，不跨越封面与卡片。"""


def adjacent_title_lines(rows: list[dict], last: dict) -> list[dict]:
    group = [last]
    for _ in range(2):
        current = group[0]
        height = max(1, current["bottom"] - current["top"])
        candidates = [r for r in rows if r is not current
                      and 0 <= current["top"] - r["bottom"] <= height * 0.65
                      and abs(r["left"] - current["left"]) <= height * 0.6
                      and 0.8 <= (r["bottom"] - r["top"]) / height <= 1.25
                      # 自动折行的前一行应比末行长，短封面标语不得拼入。
                      and r["right"] - r["left"] >= current["right"] - current["left"] - height]
        if len(candidates) != 1:
            break
        group.insert(0, candidates[0])
    return group

