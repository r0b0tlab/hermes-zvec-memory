"""Pure mirror journal validation shared by recovery and diagnostics."""


def validate_journal(value):
    if not isinstance(value, dict):
        raise ValueError("Invalid mirror map")
    schema = value.get("schema_version", 1)
    watermark = value.get("last_notification", 0)
    if type(schema) is not int or schema != 1:
        raise ValueError("Unsupported or newer mirror schema")
    if type(watermark) is not int or watermark < 0:
        raise ValueError("Invalid mirror replay watermark")
    if not isinstance(value.get("records"), dict):
        raise ValueError("Invalid mirror records")
    if not isinstance(value.get("pending_deletes"), list):
        raise ValueError("Invalid mirror deletions")
    creates = value.get("pending_creates", [])
    if not isinstance(creates, list) or type(value.get("refresh_required", False)) is not bool:
        raise ValueError("Invalid mirror journal")
    for record in value["records"].values():
        if not isinstance(record, dict) or not all(
                isinstance(record.get(key), str) for key in ("target", "content", "path")):
            raise ValueError("Invalid mirror record")
    if not all(isinstance(path, str) for path in value["pending_deletes"]):
        raise ValueError("Invalid pending mirror deletion")
    for item in creates:
        if not isinstance(item, dict) or not all(
                isinstance(item.get(key), str) for key in ("staged", "path")):
            raise ValueError("Invalid pending mirror create")
    result = dict(value)
    result.setdefault("schema_version", 1)
    result["pending_creates"] = list(creates)
    return result
