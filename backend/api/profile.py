"""用户公开主页 API：聚合公开战绩（通过题目、参赛记录与名次）。

只暴露公开数据：邮箱、最近登录时间、封禁状态等私密字段一律不返回。
与题目列表/讨论区一致，无需登录即可访问。
"""
import os

from flask import Blueprint

from backend import config
from backend.api import ok, err, find_user_by_id, find_user_by_username
from backend.storage import read_json, list_dirs, list_files
from backend.judge.ranking import get_leaderboard, contest_status

profile_bp = Blueprint("profile", __name__)

# 公开主页允许暴露的用户字段（email / last_login / is_banned 等不对外）
_PUBLIC_FIELDS = ("id", "username", "nickname", "role", "created_at")


def _find_user(key):
    """支持按用户 ID 或用户名定位用户。"""
    return find_user_by_id(key) or find_user_by_username(key)


def _iter_user_submissions(user_id):
    """遍历该用户在所有竞赛/练习分片中的提交记录。"""
    for cid in list_dirs(config.SUBMISSIONS_DIR):
        shard = read_json(os.path.join(config.SUBMISSIONS_DIR, cid, f"{user_id}.json"))
        if not shard:
            continue
        for s in shard.get("submissions", []):
            yield s


def _solved_problems(subs):
    """从提交记录汇总已通过的题目（按题目去重，记录首次 AC 时间）。"""
    first_ac = {}
    for s in subs:
        if s.get("status") != "AC":
            continue
        pid = s.get("problem_id")
        if not pid:
            continue
        ts = s.get("created_at") or ""
        if pid not in first_ac or ts < first_ac[pid]:
            first_ac[pid] = ts
    result = []
    for pid, ts in first_ac.items():
        p = read_json(os.path.join(config.PROBLEMS_DIR, f"{pid}.json"))
        result.append({
            "id": pid,
            "title": (p or {}).get("title", pid),
            "difficulty": (p or {}).get("difficulty", 0),
            "solved_at": ts or None,
        })
    result.sort(key=lambda x: (x.get("solved_at") or "", x["id"]), reverse=True)
    return result


def _contest_records(user_id):
    """汇总用户参加过的竞赛及名次（口径与公开榜单一致，含封榜处理）。"""
    records = []
    for cid in list_files(config.CONTESTS_DIR):
        c = read_json(os.path.join(config.CONTESTS_DIR, f"{cid}.json"))
        if not c or not c.get("visble", True):
            continue
        board = get_leaderboard(c, as_admin=False)
        row = next((r for r in board.get("rows", []) if r.get("user_id") == user_id), None)
        if row is None:
            continue
        records.append({
            "id": c.get("id", cid),
            "title": c.get("title", cid),
            "mode": c.get("mode", "acm"),
            "status": contest_status(c),
            "rank": row.get("rank"),
            "solved": row.get("solved", 0),
            "score": row.get("score", 0),
            "penalty": row.get("penalty", 0),
        })
    records.sort(key=lambda r: r["id"])
    return records


@profile_bp.get("/profile/<key>")
def profile(key):
    user = _find_user(key)
    if not user:
        return err("用户不存在", 404, 404)

    subs = list(_iter_user_submissions(user["id"]))
    accepted = sum(1 for s in subs if s.get("status") == "AC")
    solved = _solved_problems(subs)
    contests = _contest_records(user["id"])

    return ok({
        "user": {k: user[k] for k in _PUBLIC_FIELDS if k in user},
        "stats": {
            "solved": len(solved),
            "submissions": len(subs),
            "accepted": accepted,
            "contests": len(contests),
        },
        "solved_problems": solved,
        "contests": contests,
    })
