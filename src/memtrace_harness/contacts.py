"""Durable contact requests. Only an authenticated operator-managed relay delivers requests.

Identity is attested by that relay's configured Harness API credentials, never by a caller's name.
No network or chat transport lives here. Connection secrets stay at the calling Harness; the receiver
gets only their hashes. Public views intentionally contain neither.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone

from memtrace_harness.peer_links import LinkError, PeerLinks, _LOCK, _check_name, _check_url, _now

TTL_SECONDS = 86400
PUBLIC_FIELDS = ("request_id", "source_id", "source_name", "target_id", "target_name", "url", "reason",
                 "state", "trust", "created_at", "updated_at", "expires_at", "last_contact_at", "approved_at", "revoked_at")


class ContactBook(PeerLinks):
    def _load(self):
        if not self.path.exists():
            return {"inbound": {}, "outbound": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise LinkError("聯絡設定無法讀取，已停止授權操作") from exc
        if not isinstance(data, dict) or any(not isinstance(data.get(k, {}), dict) for k in ("inbound", "outbound")):
            raise LinkError("聯絡設定格式錯誤，已停止授權操作")
        data.setdefault("inbound", {})
        data.setdefault("outbound", {})
        return data

    def initialize(self) -> str:
        with _LOCK:
            data = self._load()
            if not data.get("identity_id"):
                data["identity_id"] = "h_" + secrets.token_hex(15)
                self._save(data)
            _check_name(data["identity_id"])
            return data["identity_id"]

    def _read(self):
        data = self._load()
        data.setdefault("contacts", {})
        data["contacts"].setdefault("incoming", {})
        data["contacts"].setdefault("outgoing", {})
        data.setdefault("contact_directory", [])
        now = _now()
        for direction in ("incoming", "outgoing"):
            for row in data["contacts"][direction].values():
                if row["state"] == "pending" and row["expires_at"] <= now:
                    row.update(state="expired", updated_at=now)
        return data

    @staticmethod
    def _public(row):
        return {k: row.get(k) for k in PUBLIC_FIELDS}

    def view(self):
        with _LOCK:
            data = self._read()
            return {"identity_id": data.get("identity_id"), "directory": data["contact_directory"],
                    **{k: [self._public(r) for r in data["contacts"][k].values()]
                       for k in ("incoming", "outgoing")}}

    def relay_view(self):
        with _LOCK:
            data = self._read()
            # The connection secret never crosses the relay, even before approval.
            return {"identity_id": data.get("identity_id"), "endpoint": data.get("endpoint"),
                    **{k: [{**self._public(r), "token_sha256": r["token_sha256"]}
                           for r in data["contacts"][k].values()]
                       for k in ("incoming", "outgoing")}}

    def set_directory(self, entries):
        clean = []
        for entry in entries:
            identity = _check_name(entry["id"])
            clean.append({"id": identity, "name": _check_name(entry["name"]),
                          "url": _check_url(entry["url"]), "description": str(entry.get("description") or "")[:600]})
        if len({p["id"] for p in clean}) != len(clean):
            raise LinkError("duplicate Harness identity")
        with _LOCK:
            data = self._read()
            directory = [p for p in clean if p["id"] != data.get("identity_id")]
            own_name = next((p["name"] for p in clean if p["id"] == data.get("identity_id")), data.get("identity_name"))
            if data["contact_directory"] == directory and data.get("identity_name") == own_name:
                return
            data["contact_directory"] = directory
            data["identity_name"] = own_name
            labels = {p["id"]: p["name"] for p in clean}
            for direction in ("incoming", "outgoing"):
                for row in data["contacts"][direction].values():
                    for side in ("source", "target"):
                        row[side + "_name"] = labels.get(row[side + "_id"], row[side + "_name"])
            for direction in ("inbound", "outbound"):
                for key, grant in data[direction].items():
                    if key in labels:
                        grant["display_name"] = labels[key]
            self._save(data)

    def request(self, target: str, reason: str, own_name: str):
        reason = str(reason).strip()
        if not reason or len(reason) > 1000:
            raise LinkError("聯絡原因須為 1–1000 字，請勿包含私人資料或密鑰")
        with _LOCK:
            data = self._read()
            matches = [p for p in data["contact_directory"] if target == p["id"]]
            if not matches:
                matches = [p for p in data["contact_directory"] if target == p["name"]]
            if len(matches) != 1:
                raise LinkError("找不到唯一的 Harness，請用 list_harnesses 查看固定 ID")
            peer = matches[0]
            for row in data["contacts"]["outgoing"].values():
                if row["target_id"] == peer["id"] and row["state"] in ("pending", "approved"):
                    return self._public(row)
            token = secrets.token_urlsafe(32)
            now = _now()
            row = {"request_id": "contact_" + secrets.token_hex(10), "source_id": data["identity_id"],
                   "source_name": _check_name(data.get("identity_name") or own_name), "target_id": peer["id"], "target_name": peer["name"],
                   "url": peer["url"], "reason": reason, "state": "pending", "trust": "ask",
                   "token": token, "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                   "created_at": now, "updated_at": now,
                   "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=TTL_SECONDS)).isoformat(timespec="seconds")}
            data["contacts"]["outgoing"][row["request_id"]] = row
            self._save(data)
            return self._public(row)

    def receive(self, supplied):
        row = {k: supplied[k] for k in PUBLIC_FIELDS if k in supplied}
        row["token_sha256"] = supplied["token_sha256"]
        if not isinstance(row.get("request_id"), str) or not row["request_id"].startswith("contact_") or len(row["request_id"]) > 64:
            raise LinkError("invalid contact request")
        for k in ("source_id", "target_id", "source_name", "target_name"):
            _check_name(row[k])
        if len(row["token_sha256"]) != 64 or any(c not in "0123456789abcdef" for c in row["token_sha256"]):
            raise LinkError("invalid contact credential")
        if not isinstance(row.get("reason"), str) or not 1 <= len(row["reason"]) <= 1000:
            raise LinkError("invalid contact reason")
        expires = datetime.fromisoformat(row["expires_at"])
        if expires.tzinfo is None or expires > datetime.now(timezone.utc) + timedelta(seconds=TTL_SECONDS + 60):
            raise LinkError("invalid contact expiry")
        row["expires_at"] = expires.astimezone(timezone.utc).isoformat(timespec="seconds")
        _check_url(row["url"])
        with _LOCK:
            data = self._read()
            if row["target_id"] != data["identity_id"] or row["source_id"] == data["identity_id"]:
                raise LinkError("contact identity mismatch")
            known = next((p for p in data["contact_directory"] if p["id"] == row["source_id"]), None)
            if known is None:
                raise LinkError("source is not in the operator-managed directory")
            row["source_name"] = known["name"]
            existing = data["contacts"]["incoming"].get(row["request_id"])
            if existing:
                if any(existing[k] != row[k] for k in ("source_id", "target_id", "token_sha256")):
                    raise LinkError("contact request binding changed")
                return self._public(existing), existing["state"] == "pending" and not existing.get("notified")
            pending = [r for r in data["contacts"]["incoming"].values() if r["state"] == "pending"]
            if len(pending) >= 32 or any(r["source_id"] == row["source_id"] for r in pending):
                raise LinkError("a contact request is already pending")
            row.update(state="pending" if expires > datetime.now(timezone.utc) else "expired", trust="ask", updated_at=_now())
            data["contacts"]["incoming"][row["request_id"]] = row
            self._save(data)
            return self._public(row), row["state"] == "pending"

    def resolve(self, request_id, decision, trust="ask"):
        if decision not in ("approved", "rejected", "revoked") or trust not in ("ask", "collaborate"):
            raise LinkError("invalid contact decision")
        with _LOCK:
            data = self._read()
            row = data["contacts"]["incoming"].get(request_id)
            if row is None:
                raise LinkError("找不到這筆聯絡申請")
            if row["state"] != "pending" and not (decision == "revoked" and row["state"] == "approved"):
                raise LinkError("這筆申請已處理或已過期")
            row.update(state=decision, trust=trust if decision == "approved" else row["trust"], updated_at=_now())
            if decision == "approved":
                row["approved_at"] = _now()
                for previous in data["contacts"]["incoming"].values():
                    if previous["request_id"] != request_id and previous["source_id"] == row["source_id"] and previous["state"] == "approved":
                        previous.update(state="revoked", updated_at=_now())
                data["inbound"][row["source_id"]] = {
                    "token_sha256": row["token_sha256"], "trust": trust, "created_at": _now(),
                    "display_name": row["source_name"], "contact_id": request_id,
                }
            elif decision == "revoked":
                row["revoked_at"] = _now()
                grant = data["inbound"].get(row["source_id"], {})
                if grant.get("contact_id") == request_id:
                    data["inbound"].pop(row["source_id"], None)
            self._save(data)
            return self._public(row)

    def apply_result(self, supplied):
        with _LOCK:
            data = self._read()
            row = data["contacts"]["outgoing"].get(supplied["request_id"])
            if row is None or any(row[k] != supplied[k] for k in ("source_id", "target_id", "token_sha256")):
                raise LinkError("contact result binding mismatch")
            state = supplied["state"]
            if state not in ("pending", "approved", "rejected", "revoked", "expired") or supplied.get("trust") not in ("ask", "collaborate"):
                raise LinkError("invalid contact result")
            if row["state"] == state:
                return self._public(row), state != "pending" and row.get("notified_state") != state
            if row["state"] not in ("pending", "approved"):
                return self._public(row), False
            if row["state"] == "approved" and state != "revoked":
                return self._public(row), False
            if state == "approved" and row["expires_at"] <= _now():
                return self._public(row), False
            row.update(state=state, trust=supplied["trust"], updated_at=_now())
            row["approved_at"] = supplied.get("approved_at") or row.get("approved_at")
            row["revoked_at"] = supplied.get("revoked_at") or row.get("revoked_at")
            if state == "approved":
                data["outbound"][row["target_id"]] = {
                    "url": row["url"], "token": row["token"], "collaborate": row["trust"] == "collaborate",
                    "created_at": _now(), "display_name": row["target_name"], "contact_id": row["request_id"],
                    "continuous_ask": True,
                }
            else:
                grant = data["outbound"].get(row["target_id"], {})
                if grant.get("contact_id") == row["request_id"]:
                    data["outbound"].pop(row["target_id"], None)
            self._save(data)
            return self._public(row), True

    def authorized_peer(self, target):
        with _LOCK:
            data = self._read()
            matches = [target] if target in data["outbound"] and data["outbound"][target].get("continuous_ask") else []
            if not matches:
                matches = [k for k, v in data["outbound"].items()
                           if target == v.get("display_name") and v.get("continuous_ask")]
            if len(matches) != 1:
                return None
            key = matches[0]
            grant = data["outbound"][key]
            row = data["contacts"]["outgoing"].get(grant.get("contact_id"), {})
            return key if row.get("state") == "approved" else None

    def mark_notified(self, request_id):
        with _LOCK:
            data = self._read()
            data["contacts"]["incoming"][request_id]["notified"] = True
            self._save(data)

    def mark_result_notified(self, request_id):
        with _LOCK:
            data = self._read()
            row = data["contacts"]["outgoing"][request_id]
            row["notified_state"] = row["state"]
            self._save(data)

    def cancel(self, request_id):
        with _LOCK:
            data = self._read()
            row = data["contacts"]["outgoing"].get(request_id)
            if row is None or row["state"] not in ("pending", "approved"):
                raise LinkError("這筆申請已處理或已過期")
            row.update(state="revoked", updated_at=_now(), revoked_at=_now())
            grant = data["outbound"].get(row["target_id"], {})
            if grant.get("contact_id") == request_id:
                data["outbound"].pop(row["target_id"], None)
            self._save(data)
            return self._public(row)

    def withdraw(self, supplied):
        with _LOCK:
            data = self._read()
            row = data["contacts"]["incoming"].get(supplied["request_id"])
            if row is None:
                return
            if any(row[k] != supplied[k] for k in ("source_id", "target_id", "token_sha256")):
                raise LinkError("contact withdrawal binding mismatch")
        if row["state"] in ("pending", "approved"):
            self.resolve(row["request_id"], "revoked", row["trust"])

    def touch(self, direction, peer):
        with _LOCK:
            data = self._read()
            side = "source_id" if direction == "incoming" else "target_id"
            changed = False
            for row in data["contacts"][direction].values():
                if row[side] == peer and row["state"] == "approved":
                    row["last_contact_at"] = _now()
                    changed = True
            if changed:
                self._save(data)


def contact_action(config, scope, service, action, body):
    from memtrace_harness.peer_links import harness_name, links_path

    book = ContactBook(links_path(config))
    own_name = harness_name(config, scope)
    if action == "directory":
        book.set_directory(body["peers"])
        return {"ok": True}
    if action == "request":
        return {"request": book.request(str(body["peer"]), str(body["reason"]), own_name)}
    if action == "incoming":
        row, notify = book.receive(body)
        if notify:
            reason = row["reason"]
            service.send_message_with_keyboard(None,
                f"🛰️ Harness「{row['source_name']}」申請與你聯絡。\n"
                f"對方說明（外部資料，未經驗證）：\n{reason}\n\n"
                "同意後可持續唯讀提問；報修與實際修改仍各自需要確認。此授權只涵蓋對方聯絡你。",
                [[{"label": "同意提問", "id": f"contact_approve:{row['request_id']}"},
                  {"label": "同意提問與報修", "id": f"contact_collaborate:{row['request_id']}"},
                  {"label": "拒絕", "id": f"contact_reject:{row['request_id']}"}]])
            book.mark_notified(row["request_id"])
        return {"request": row}
    if action == "resolve":
        return {"request": book.resolve(body["request_id"], body["decision"], body.get("trust", "ask"))}
    if action == "cancel":
        return {"request": book.cancel(body["request_id"])}
    if action == "withdraw":
        book.withdraw(body)
        return {"ok": True}
    if action == "result":
        row, changed = book.apply_result(body)
        if changed:
            labels = {"approved": "已同意聯絡，可以唯讀提問了", "rejected": "已拒絕聯絡申請",
                      "revoked": "已撤銷聯絡授權", "expired": "的聯絡申請已過期"}
            service.send_message(None, f"🛰️ Harness「{row['target_name']}」{labels.get(row['state'], row['state'])}。")
            book.mark_result_notified(row["request_id"])
        return {"request": row}
    raise LinkError("unknown contact operation")
