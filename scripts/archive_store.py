#!/usr/bin/env python3
"""下架资费永久存档：加密格式与合并逻辑。

设计要点
--------
* 存档库存「所有曾经出现过的资费」——因为一条资费的变化发生在它下架之前的
  在售期，只有一直跟踪它，才能在它下架后仍保留完整变化记录。检索页默认只
  展示「已下架」，可一键切到「全部」，兼顾用户「只看下架」与「变化记录不丢」。
* 每条记录：完整字段快照 + 状态(在售/已下架) + 首次/最后出现日 + 永久累积的
  变化历史 h[]。h[] 每轮由 current_changes.json 追加并去重，不随 history.json
  的 150 条轮转丢失。
* 加密：PBKDF2-HMAC-SHA256 + AES-GCM，与浏览器 Web Crypto 原生兼容。

二进制文件格式（archive.bin）
----------------------------
    b"ARC1" | salt(16) | iv(12) | tag(16) | ciphertext
明文为 UTF-8 JSON，字段尽量短以控制体积（分源加载，单源一般 1-3 MB）。
"""
import base64
import hashlib
import json
import os
from datetime import datetime, timezone

MAGIC = b"ARC1"
PBKDF2_ITERS = 200_000
SALT_LEN = 16
IV_LEN = 12
TAG_LEN = 16
VAULT_VERSION = 1

# 在售资费只存精简索引（线上 items.json 已有完整数据，无需重复）。
KEEP_FIELDS = (
    "id", "name", "code", "op", "type", "owner", "province",
    "price", "data", "voice", "broadband", "date",
)
# 已下架资费才额外保留的完整字段（这是要永久留存的「副本」）。
FULL_FIELDS = (
    "note", "note_full", "scope", "region", "channel", "source",
)


# ---------------------------------------------------------------- 密钥/加解密
def _derive_key(password: str, salt: bytes) -> bytes:
    from Crypto.Protocol.KDF import PBKDF2
    from Crypto.Hash import SHA256
    return PBKDF2(
        password.encode("utf-8"), salt,
        dkLen=32, count=PBKDF2_ITERS, hmac_hash_module=SHA256,
    )


def encrypt(plain: bytes, password: str) -> bytes:
    from Crypto.Cipher import AES
    from Crypto.Random import get_random_bytes
    salt = get_random_bytes(SALT_LEN)
    iv = get_random_bytes(IV_LEN)
    key = _derive_key(password, salt)
    cipher = AES.new(key, AES.MODE_GCM, nonce=iv, mac_len=TAG_LEN)
    ct, tag = cipher.encrypt_and_digest(plain)
    return MAGIC + salt + iv + tag + ct


def decrypt(blob: bytes, password: str) -> bytes:
    from Crypto.Cipher import AES
    if not blob.startswith(MAGIC):
        raise ValueError("存档文件格式不正确（缺少 ARC1 头）")
    off = len(MAGIC)
    salt = blob[off:off + SALT_LEN]; off += SALT_LEN
    iv = blob[off:off + IV_LEN]; off += IV_LEN
    tag = blob[off:off + TAG_LEN]; off += TAG_LEN
    ct = blob[off:]
    key = _derive_key(password, salt)
    cipher = AES.new(key, AES.MODE_GCM, nonce=iv, mac_len=TAG_LEN)
    return cipher.decrypt_and_verify(ct, tag)  # 密码错误会抛 ValueError


def read_vault(path: str, password: str) -> dict:
    if not os.path.exists(path):
        return new_vault("")
    with open(path, "rb") as f:
        raw = f.read()
    try:
        plain = decrypt(raw, password)
    except ValueError as e:
        raise RuntimeError("存档解密失败（密码不对或文件损坏）：%s" % e)
    return json.loads(plain.decode("utf-8"))


def write_vault(path: str, vault: dict, password: str) -> None:
    plain = json.dumps(vault, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    blob = encrypt(plain, password)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(blob)
    os.replace(tmp, path)


# ---------------------------------------------------------------- 库结构
def new_vault(op: str) -> dict:
    return {
        "v": VAULT_VERSION,
        "op": op,
        "updated": "",
        "records": {},   # key -> record
    }


def _slim_item(d: dict, offline: bool = False) -> dict:
    """在售只存精简索引；offline=True（已下架）才保留完整副本含 details。"""
    fields = KEEP_FIELDS + (FULL_FIELDS if offline else ())
    out = {}
    for k in fields:
        if k in d and d[k] not in (None, "", [], {}):
            out[k] = d[k]
    if offline:
        det = d.get("details")
        if isinstance(det, dict):
            det2 = {k: v for k, v in det.items() if v not in (None, "", [], {})}
            if det2:
                out["details"] = det2
    return out


def _rec_key(d: dict) -> str:
    """与 build_data.stable_key 保持一致（避免循环导入，这里内联同样逻辑）。"""
    code = str(d.get("code") or "").strip()
    if code:
        return code
    return "%s|%s|%s|%s" % (
        d.get("op", ""), d.get("name", ""),
        d.get("owner", ""), d.get("province", ""),
    )


def _change_sig(c: dict) -> str:
    """变化去重签名：同一天、同类、同样 old 值只记一次。"""
    old = c.get("old")
    old_s = json.dumps(old, ensure_ascii=False, sort_keys=True) if isinstance(old, dict) else str(old or "")
    raw = "%s|%s|%s" % (c.get("date", ""), c.get("kind", ""), old_s)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _append_history(rec: dict, changes: list) -> int:
    """把本轮变化追加进 record.h[]，按签名去重。返回新增条数。"""
    h = rec.setdefault("h", [])
    seen = {e.get("s") for e in h if isinstance(e, dict)}
    added = 0
    for c in changes:
        sig = _change_sig(c)
        if sig in seen:
            continue
        entry = {"s": sig, "d": c.get("date", ""), "k": c.get("kind", "")}
        old = c.get("old")
        if isinstance(old, dict) and old:
            entry["f"] = old          # 修改后的旧值，便于回溯「当时是什么」
        h.append(entry)
        seen.add(sig)
        added += 1
    return added


def merge(vault: dict, items: list, changes: list, today: str, op: str) -> dict:
    """把本轮 items（当前在售）与 changes（本轮变化）并入存档库。"""
    if not isinstance(vault, dict) or not isinstance(vault.get("records"), dict):
        vault = new_vault(op)
    vault["op"] = op or vault.get("op", "")
    vault["v"] = VAULT_VERSION
    recs = vault["records"]

    # 1) 本轮在售：新建或更新快照、最后出现日、状态
    for d in items:
        if not isinstance(d, dict):
            continue
        k = _rec_key(d)
        rec = recs.get(k)
        if rec is None:
            rec = {
                "k": k, "n": d.get("name", ""), "op": d.get("op", op),
                "p": d.get("province", "") or "全国",
                "fs": today, "ls": today, "st": "在售", "i": _slim_item(d), "h": [],
            }
            recs[k] = rec
        else:
            rec["st"] = "在售"
            rec["ls"] = today
            rec["n"] = d.get("name") or rec.get("n", "")
            # 曾下架过又重新上架：保留已有的完整副本，不用精简快照覆盖
            if "os" not in rec:
                rec["i"] = _slim_item(d)

    # 2) 本轮变化：按 key 累积历史；下架的落状态与下架日期、快照
    by_key = {}
    for c in changes:
        if not isinstance(c, dict):
            continue
        by_key.setdefault(_rec_key(c), []).append(c)

    for k, cs in by_key.items():
        rec = recs.get(k)
        if rec is None:
            first = cs[0]
            rec = {
                "k": k, "n": first.get("name", ""), "op": first.get("op", op),
                "p": first.get("province", "") or "全国",
                "fs": first.get("date", today), "ls": first.get("date", today),
                "st": "在售", "i": _slim_item(first), "h": [],
            }
            recs[k] = rec
        _append_history(rec, cs)
        # 记录首次出现日（若历史里有更早的新增）
        for c in cs:
            if c.get("kind") == "新增" and c.get("date", today) < rec.get("fs", today):
                rec["fs"] = c["date"]
            if c.get("kind") == "下架":
                rec["st"] = "已下架"
                rec["os"] = c.get("date", today)
                rec["ls"] = c.get("date", rec.get("ls", today))
                # 下架时的完整快照 —— 这是「下架副本」的核心，含全部公示详情
                rec["i"] = _slim_item(c, offline=True)

    vault["updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return vault


def stats(vault: dict) -> dict:
    recs = vault.get("records") or {}
    offline = sum(1 for r in recs.values() if r.get("st") == "已下架")
    hist = sum(len(r.get("h") or []) for r in recs.values())
    return {"total": len(recs), "offline": offline, "history": hist}
