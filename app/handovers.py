"""装船完结交接：不可变凭证的纯计算部分（实绩指纹与逐箱完工延误）。

凭证签发前的全部业务判断在服务层的写事务内完成；本模块只提供无副作用的
规范化指纹与快照组装，使“签发时固化的指纹”与“查询时重算的当前指纹”严格
使用同一规范：

* 指纹覆盖**实绩版本**、**冻结排程基线整表快照**与**全部有效事件的现行内容**
  （流水号、类型、箱号、现行岸桥、现行分钟；不覆盖首次登记原值/更正标记/
  登记时刻，因为凭证只对“当前有效实绩”负责）；
* 规范化 JSON：``sort_keys``、无空白、UTF-8 不取 ASCII 转义，再取 SHA-256。

后续补登（新流水号）或更正（分钟/岸桥变化、版本推进）都会改变指纹；
单纯的方案状态变化（confirmed → superseded）不改变指纹——那属于方案生命周期，
不属于实绩变化。
"""

from __future__ import annotations

import hashlib
import json


def canonical_actuals(
    *,
    version: int,
    baseline_rows,
    event_rows,
) -> str:
    """返回实绩快照的规范化 JSON 文本。

    :param version: 实绩版本（首次登记为 1，每次更正 +1）。
    :param baseline_rows: ``(container_no, stack, tier, start, finish, crane_no)``
        可迭代对象（冻结排程基线逐箱快照）。
    :param event_rows: ``(seq, event_type, container_no, crane_no, minute)``
        可迭代对象（全部**现行有效**事件，更正后为替代值）。
    """
    baseline = sorted(
        [list(r[:6]) for r in baseline_rows],
        key=lambda r: r[0],
    )
    events = sorted(
        [list(r[:5]) for r in event_rows],
        key=lambda r: r[0],
    )
    payload = {"version": version, "baseline": baseline, "events": events}
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def actuals_fingerprint(
    *,
    version: int,
    baseline_rows,
    event_rows,
) -> str:
    """计算实绩指纹：规范化 JSON 的 SHA-256 十六进制摘要。"""
    canonical = canonical_actuals(
        version=version, baseline_rows=baseline_rows, event_rows=event_rows
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def finish_delay(actual_finish_minute: int, baseline_finish_minute: int) -> int:
    """逐箱完工延误（分钟）：实际完工相对冻结基线的延后量，提前不计为延误。"""
    return max(actual_finish_minute - baseline_finish_minute, 0)
