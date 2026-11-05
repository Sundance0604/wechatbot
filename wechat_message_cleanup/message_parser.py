"""Extracted from the verified v1 exporter; legacy semantics preserved."""
import re


RED_PACKET_TYPE = 0x7D100000031


TYPE_LABELS = {
    1: "文本",
    3: "图片",
    34: "语音",
    43: "视频",
    47: "动画表情",
    48: "位置",
    49: "文件/链接/卡片",
    50: "音视频通话",
    10000: "系统消息",
    11000: "动画表情",
}


def get_base_type(local_type):
    if local_type == RED_PACKET_TYPE:
        return RED_PACKET_TYPE

    if local_type in TYPE_LABELS:
        return local_type

    if isinstance(local_type, int):
        low32 = local_type & 0xFFFFFFFF

        if low32 in TYPE_LABELS:
            return low32

        low8 = low32 & 0xFF

        if low8 in TYPE_LABELS:
            return low8

    return local_type


def type_label(local_type):
    if local_type == RED_PACKET_TYPE:
        return "红包"

    base = get_base_type(local_type)

    return TYPE_LABELS.get(
        base,
        f"类型{local_type}"
    )


def decode_payload(value):
    if value is None:
        return ""

    if isinstance(value, str):
        return value

    if not isinstance(value, bytes):
        return str(value)

    # 普通 UTF-8
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        pass

    # 微信 4.x 很多 XML 使用 ZSTD
    magic = b"\x28\xb5\x2f\xfd"
    pos = value.find(magic)

    if pos >= 0:
        try:
            import zstandard as zstd

            dctx = zstd.ZstdDecompressor()

            result = dctx.decompress(
                value[pos:],
                max_output_size=5_000_000
            )

            return result.decode(
                "utf-8",
                errors="ignore"
            )

        except Exception:
            pass

    return ""


def extract_xml_text(text, tag):
    if not text:
        return ""

    patterns = [
        rf"<{tag}>"
        rf"<!\[CDATA\[(.*?)\]\]>"
        rf"</{tag}>",

        rf"<{tag}>(.*?)</{tag}>",
    ]

    for p in patterns:

        m = re.search(
            p,
            text,
            re.I | re.S
        )

        if m:

            value = re.sub(
                r"<[^>]+>",
                "",
                m.group(1)
            )

            return " ".join(
                value.split()
            ).strip()

    return ""


def summarize_system(text):
    if not text:
        return "[系统消息]"

    content = extract_xml_text(
        text,
        "content"
    )

    if content:
        return f"[系统消息] {content}"

    return "[系统消息]"


def summarize_card(text):
    if not text:
        return "[文件/链接/卡片]"

    title = extract_xml_text(
        text,
        "title"
    )

    des = extract_xml_text(
        text,
        "des"
    )

    if title and des:
        return (
            f"[卡片] {title} - {des}"
        )

    if title:
        return f"[卡片] {title}"

    if des:
        return f"[卡片] {des}"

    return "[文件/链接/卡片]"


def summarize_red_packet(text):
    des = extract_xml_text(
        text,
        "des"
    )

    if des:
        return f"[红包] {des}"

    title = extract_xml_text(
        text,
        "sendertitle"
    )

    if title:
        return f"[红包] {title}"

    return "[红包]"


def clean_group_content(
    row,
    known_ids,
    group_wxid
):
    from .sender_resolver import sender_prefix

    local_type = row.get(
        "local_type"
    )

    base_type = get_base_type(
        local_type
    )

    raw = decode_payload(
        row.get("message_content")
    )

    compress = decode_payload(
        row.get("compress_content")
    )

    # 优先使用 message_content
    text = raw or compress or ""

    # 去掉 wxid 前缀
    _, cut = sender_prefix(
        text,
        known_ids,
        group_wxid
    )

    if cut:
        text = text.lstrip(
            "\ufeff"
        )[cut:].strip()

    # 文本
    if base_type == 1:

        if text:
            return text

        return "[文本]"

    # 媒体
    if base_type == 3:
        return "[图片]"

    if base_type == 34:
        return "[语音]"

    if base_type == 43:
        return "[视频]"

    if base_type in (
        47,
        11000
    ):
        return "[动画表情]"

    if base_type == 48:
        return "[位置]"

    if base_type == 50:
        return "[音视频通话]"

    if base_type == 10000:
        return summarize_system(
            text
        )

    if local_type == RED_PACKET_TYPE:
        return summarize_red_packet(
            text or compress
        )

    if base_type == 49:
        return summarize_card(
            text or compress
        )

    # 未知类型：短文本可以保留
    if (
        text
        and len(text) < 500
        and not text.lstrip().startswith("<")
    ):
        return text

    return f"[{type_label(local_type)}]"
