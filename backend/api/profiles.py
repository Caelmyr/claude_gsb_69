"""用户公开主页 API。

只暴露公开战绩数据：通过题目、提交概览、参赛记录与名次；
不返回邮箱、密码哈希、封禁状态、IP、源代码等私密信息。
无需登录即可访问（主页本身是公开的）。
"""
import os
from collections import Counter

from flask import Blueprint

from backend import config
from backend.api import ok, err, find_user_by_id, find_user_by_username
from backend.storage import read_json, list_files, list_dirs
from backend.judge.ranking import get_leaderboard, contest_status

profiles_bp = Blueprint("profiles", __name__)

# 仅白名单字段对外公开（刻意不含 email / password_hash / salt / is_banned / last_login）
PUBLIC_USER_FIELDS = ("id", "username", "nickname", "role", "created_at")
RECENT_LIMIT = 20

# 近期提交列表中允许公开的字段（刻意不含 code / ip）
PUBLIC_SUB_FIELDS = ("id", "contest_id", "problem_id", "language", "status",
                     "score", "time_ms", "memory_kb", "created_at")


def _public_basic(user):
    return {k: user[k] for k in PUBLIC_USER_FIELDS if k in user}


def _contest_visible(c):
    # 兼容历史数据中可能出现的 visble 拼写
    return bool(c.get("visible", c.get("visble", True)))


def _resolve_user(key):
    """优先按用户 ID 查找，找不到再按用户名查找。"""
    user = find_user_by_id(key)
    if user:
        return user
    return find_user_by_username(key)


def _iter_user_submissions(user_id):
    """遍历该用户在全部竞赛/练习分片中的提交。"""
    for cid in list_dirs(config.SUBMISSIONS_DIR):
        path = os.path.join(config.SUBMISSIONS_DIR, cid, f"{user_id}.json")
        shard = read_json(path)
        if not shard:
            continue
        for s in shard.get("submissions", []):
            if s.get("user_id") == user_id:
                yield cid, s


def _build_submission_summary(user_id):
    """聚合提交：题目通过情况、判定分布、语言分布、近期提交。"""
    problem_stats = {}      # problem_id -> 聚合信息
    verdict_counter = Counter()
    lang_counter = Counter()
    total = 0
    accepted = 0
    recent = []

    for _cid, s in _iter_user_submissions(user_id):
        total += 1
        status = s.get("status", "PENDING")
        verdict_counter[status] += 1
        lang_counter[s.get("language", "?")] += 1
        if status == "AC":
            accepted += 1

        pid = s.get("problem_id")
        if pid:
            p = problem_stats.setdefault(pid, {
                "problem_id": pid, "attempts": 0, "solved": False,
                "best_score": 0, "first_ac_at": None, "last_submitted_at": None,
            })
            p["attempts"] += 1
            created = s.get("created_at") or ""
            if not p["last_submitted_at"] or created > p["last_submitted_at"]:
                p["last_submitted_at"] = created
            if status == "AC":
                p["solved"] = True
                try:
                    p["best_score"] = max(p["best_score"], int(s.get("score") or 0))
                except (TypeError, ValueError):
                    pass
                if not p["first_ac_at"] or created < p["first_ac_at"]:
                    p["first_ac_at"] = created

        recent.append({k: s.get(k) for k in PUBLIC_SUB_FIELDS})

    # 已通过题目（按首次通过时间升序，与刷题进度一致）
    solved_problems = [p for p in problem_stats.values() if p["solved"]]
    solved_problems.sort(key=lambda p: p.get("first_ac_at") or "")
    # 补充题目标题/难度（题目可能已被删除，此时标题为 None）
    for p in solved_problems:
        problem = read_json(os.path.join(config.PROBLEMS_DIR, f"{p['problem_id']}.json"))
        p["title"] = problem.get("title") if problem else None
        p["difficulty"] = problem.get("difficulty") if problem else None

    recent.sort(key=lambda s: s.get("created_at") or "", reverse=True)
    recent = recent[:RECENT_LIMIT]

    return {
        "total_submissions": total,
        "accepted_submissions": accepted,
        "attempted_problems": len(problem_stats),
        "solved_count": len(solved_problems),
        "solved_problems": solved_problems,
        "verdict_counts": dict(verdict_counter),
        "language_counts": dict(lang_counter),
        "recent_submissions": recent,
    }


def _build_contest_records(user_id):
    """读取该用户在各公开竞赛中的成绩分片与最终名次。"""
    records = []
    for cid in list_dirs(config.SCORES_DIR):
        if cid == "practice":
            continue  # 练习模式不计入参赛记录
        rec = read_json(os.path.join(config.SCORES_DIR, cid, f"{user_id}.json"))
        if not rec:
            continue
        contest = read_json(os.path.join(config.CONTESTS_DIR, f"{cid}.json"))
        if not contest or not _contest_visible(contest):
            continue

        solved = 0
        for p in rec.get("problems", {}).values():
            if p.get("solved"):
                solved += 1

        # 名次取自公开榜单（封榜期间非管理员看到的是冻结快照）
        rank = None
        total_participants = 0
        frozen = False
        try:
            board = get_leaderboard(contest, as_admin=False)
            frozen = bool(board.get("frozen"))
            rows = board.get("rows", [])
            total_participants = len(rows)
            row = next((r for r in rows if r.get("user_id") == user_id), None)
            if row is not None:
                rank = row.get("rank")
        except Exception:
            pass

        records.append({
            "contest_id": cid,
            "title": contest.get("title", cid),
            "mode": contest.get("mode", "acm"),
            "status": contest_status(contest),
            "start_time": contest.get("start_time"),
            "end_time": contest.get("end_time"),
            "rank": rank,
            "total_participants": total_participants,
            "frozen": frozen and rank is None,
            "solved": solved,
            "score": rec.get("score", 0),
            "penalty": rec.get("penalty", 0),
        })

    records.sort(key=lambda r: r.get("start_time") or "", reverse=True)
    return records


@profiles_bp.get("/profiles/<user_key>")
def get_profile(user_key):
    user = _resolve_user(user_key)
    if not user:
        return err("用户不存在", 404, 404)
    data = _public_basic(user)
    data.update(_build_submission_summary(user["id"]))
    data["contests"] = _build_contest_records(user["id"])
    data["contest_count"] = len(data["contests"])
    return ok(data)
