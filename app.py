"""쇼핑 리스트 앱 (Streamlit). 요구사항은 prd.md 참고."""

import json
import os
import re
import shutil
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable

import streamlit as st

DATA_FILE = Path(__file__).parent / "shopping_list.json"
NAME_MAX_LEN = 50

FILTER_ALL = "전체"
FILTER_TODO = "미구매"
FILTER_DONE = "구매 완료"


# =============================================================================
# 데이터 계층
# 아이템 구조 (PRD 4.1):
#   id: str (UUID), name: str, quantity: int, checked: bool, created_at: str (ISO 8601)
#
# 동시성: 모든 Streamlit 세션(탭)은 한 프로세스에서 실행되므로, 프로세스 전역 락 아래에서
# "파일 읽기 → 변경 → 저장"을 수행한다. 세션이 들고 있는 목록으로 파일을 덮어쓰지 않으므로
# 여러 탭·기기에서 동시에 사용해도 다른 세션의 변경이 사라지지 않는다.
# =============================================================================


@st.cache_resource
def _file_lock() -> threading.RLock:
    """모든 세션이 공유하는 파일 락 (스크립트 재실행 사이에도 같은 객체)."""
    return threading.RLock()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _to_bool(value) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in ("true", "false", "1", "0"):
        return value.strip().lower() in ("true", "1")
    return None


def _to_quantity(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return max(1, value)
    if isinstance(value, float) and value.is_integer():
        return max(1, int(value))
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return max(1, int(value.strip()))
    return None


def normalize_item(raw, seen_ids: set[str]) -> dict | None:
    """파일에서 읽은 아이템을 검증·정규화한다. 이름이 없는 등 복구 불가하면 None."""
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    item_id = raw.get("id")
    if not isinstance(item_id, str) or not item_id or item_id in seen_ids:
        item_id = str(uuid.uuid4())
    seen_ids.add(item_id)
    quantity = _to_quantity(raw.get("quantity"))
    checked = _to_bool(raw.get("checked"))
    created_at = raw.get("created_at")
    return {
        "id": item_id,
        "name": name.strip()[:NAME_MAX_LEN],
        "quantity": 1 if quantity is None else quantity,
        "checked": False if checked is None else checked,
        "created_at": created_at if isinstance(created_at, str) and created_at else _now(),
    }


def _backup_path() -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return DATA_FILE.with_name(f"{DATA_FILE.stem}.corrupt-{stamp}{DATA_FILE.suffix}")


def save_items(items: list[dict]) -> None:
    """목록을 원자적으로 저장한다 (임시 파일에 쓴 뒤 교체 → 저장 중 중단돼도 파일이 깨지지 않음)."""
    tmp = DATA_FILE.with_name(DATA_FILE.name + ".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, DATA_FILE)


def load_items() -> tuple[list[dict], str | None]:
    """파일에서 목록을 불러온다. (목록, 사용자 안내 메시지) 반환.

    - 파일 없음: 빈 목록
    - JSON 문법 오류 / 최상위가 리스트가 아님: 원본을 백업 파일로 옮기고 빈 목록
    - 일부 아이템 형식 오류: 원본을 백업한 뒤 정규화된 목록으로 다시 저장
    """
    with _file_lock():
        if not DATA_FILE.exists():
            return [], None
        try:
            raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raw = None
        except OSError as exc:
            return [], f"데이터 파일을 읽지 못했습니다: {exc}"

        if not isinstance(raw, list):
            backup = _backup_path()
            os.replace(DATA_FILE, backup)
            return [], f"데이터 파일이 손상되어 '{backup.name}'(으)로 백업하고 빈 목록으로 시작합니다."

        seen: set[str] = set()
        items = [it for it in (normalize_item(r, seen) for r in raw) if it is not None]
        if items == raw:
            return items, None

        backup = _backup_path()
        shutil.copyfile(DATA_FILE, backup)
        save_items(items)
        dropped = len(raw) - len(items)
        detail = f"형식이 잘못된 아이템 {dropped}개를 제외하고" if dropped else "잘못된 값을 바로잡고"
        return items, f"데이터 파일에서 {detail} 불러왔습니다. (원본 백업: '{backup.name}')"


def _items() -> list[dict]:
    return st.session_state["items"]


def _mutate(apply: Callable[[list[dict]], tuple[list[dict], str | None]]) -> str | None:
    """락 아래에서 최신 파일을 읽고 apply로 변경한 뒤 저장한다.

    apply(items) -> (새 목록, 오류 메시지). 오류가 있으면 저장하지 않는다.
    반환값: 사용자에게 보여 줄 오류 메시지 또는 None.
    """
    with _file_lock():
        items, notice = load_items()
        if notice:
            st.session_state["notice"] = notice
        new_items, error = apply(items)
        if error is None:
            try:
                save_items(new_items)
                items = new_items
            except OSError as exc:
                error = f"저장에 실패했습니다: {exc}"
    st.session_state["items"] = items
    return error


def validate_name(items: list[dict], name: str, exclude_id: str | None = None) -> str | None:
    """이름 유효성 검사. 문제가 있으면 오류 메시지를, 없으면 None을 반환한다."""
    if not name:
        return "아이템 이름을 입력해 주세요."
    if len(name) > NAME_MAX_LEN:
        return f"아이템 이름은 {NAME_MAX_LEN}자 이하로 입력해 주세요."
    key = name.casefold()
    for item in items:
        if item["id"] != exclude_id and item["name"].strip().casefold() == key:
            return f"'{item['name']}'은(는) 이미 목록에 있습니다."
    return None


def add_item(name: str, quantity: int) -> str | None:
    """아이템을 추가한다. 실패 시 오류 메시지를 반환한다."""
    name = name.strip()

    def apply(items):
        error = validate_name(items, name)
        if error:
            return items, error
        return items + [{
            "id": str(uuid.uuid4()),
            "name": name,
            "quantity": max(1, int(quantity)),
            "checked": False,
            "created_at": _now(),
        }], None

    return _mutate(apply)


def update_item(item_id: str, name: str, quantity: int) -> str | None:
    """아이템 이름과 수량을 수정한다. 실패 시 오류 메시지를 반환한다."""
    name = name.strip()

    def apply(items):
        if not any(item["id"] == item_id for item in items):
            return items, "이미 삭제된 아이템입니다."
        error = validate_name(items, name, exclude_id=item_id)
        if error:
            return items, error
        return [
            {**item, "name": name, "quantity": max(1, int(quantity))}
            if item["id"] == item_id else item
            for item in items
        ], None

    return _mutate(apply)


def delete_item(item_id: str) -> str | None:
    return _mutate(lambda items: ([i for i in items if i["id"] != item_id], None))


def set_checked(item_id: str, checked: bool) -> str | None:
    """체크 상태를 지정한 값으로 설정한다 (토글이 아니라 설정이므로 연타·동시 사용에도 안전)."""
    return _mutate(lambda items: (
        [{**i, "checked": checked} if i["id"] == item_id else i for i in items], None
    ))


def clear_checked() -> int:
    """구매 완료 항목을 모두 삭제하고 삭제한 개수를 반환한다."""
    removed = 0

    def apply(items):
        nonlocal removed
        remaining = [i for i in items if not i["checked"]]
        removed = len(items) - len(remaining)
        return remaining, None

    error = _mutate(apply)
    if error:
        st.session_state["notice"] = error
        return 0
    return removed


def init_state() -> None:
    """매 실행마다 파일에서 최신 목록을 읽어 다른 탭·기기의 변경을 반영한다."""
    for key, default in [("editing_id", None), ("add_error", None),
                         ("edit_error", None), ("notice", None), ("form_ver", 0)]:
        st.session_state.setdefault(key, default)
    items, notice = load_items()
    st.session_state["items"] = items
    if notice:
        st.session_state["notice"] = notice
    # 편집 중이던 아이템이 다른 곳에서 삭제됐으면 편집 모드 해제
    editing_id = st.session_state["editing_id"]
    if editing_id is not None and editing_id not in {i["id"] for i in items}:
        st.session_state["notice"] = "수정 중이던 아이템이 다른 곳에서 삭제되었습니다."
        st.session_state["editing_id"] = None
        st.session_state["edit_error"] = None


# =============================================================================
# 콜백 (위젯 이벤트 → 데이터 계층 호출)
# =============================================================================


def on_add() -> None:
    ver = st.session_state["form_ver"]
    error = add_item(st.session_state[f"new_name_{ver}"], st.session_state[f"new_qty_{ver}"])
    st.session_state["add_error"] = error
    if not error:
        # 성공했을 때만 입력창을 비운다 (거부 시에는 입력값 유지).
        # 위젯 값을 직접 덮어쓰면 범위 밖 수량 입력 등에서 프론트엔드와 상태가 어긋나므로,
        # key 버전을 올려 새 위젯(기본값)으로 교체한다.
        st.session_state["form_ver"] = ver + 1
        st.toast("추가되었습니다", icon="✅")


def on_start_edit(item: dict) -> None:
    st.session_state["editing_id"] = item["id"]
    st.session_state["edit_error"] = None
    # 편집 입력창에 현재 값을 채워 둔다 (위젯 생성 전이므로 설정 가능).
    st.session_state[f"edit_name_{item['id']}"] = item["name"]
    st.session_state[f"edit_qty_{item['id']}"] = item["quantity"]


def on_save_edit(item_id: str) -> None:
    error = update_item(
        item_id,
        st.session_state[f"edit_name_{item_id}"],
        st.session_state[f"edit_qty_{item_id}"],
    )
    st.session_state["edit_error"] = error
    if not error:
        st.session_state["editing_id"] = None
        st.toast("수정되었습니다", icon="✏️")


def on_cancel_edit() -> None:
    st.session_state["editing_id"] = None
    st.session_state["edit_error"] = None


def on_toggle(item_id: str) -> None:
    error = set_checked(item_id, st.session_state[f"check_{item_id}"])
    if error:
        st.session_state["notice"] = error


def on_delete(item_id: str) -> None:
    error = delete_item(item_id)
    if error:
        st.session_state["notice"] = error
        return
    if st.session_state["editing_id"] == item_id:
        on_cancel_edit()
    st.toast("삭제되었습니다", icon="🗑️")


def on_clear_checked() -> None:
    removed = clear_checked()
    if st.session_state["editing_id"] not in {i["id"] for i in _items()}:
        on_cancel_edit()
    st.toast(f"구매 완료 항목 {removed}개를 삭제했습니다", icon="🧹")


# =============================================================================
# UI
# =============================================================================


def escape_markdown(text: str) -> str:
    """사용자 입력이 마크다운으로 해석되지 않도록 특수문자를 이스케이프한다.

    일반 마크다운 기호에 더해 Streamlit 전용 문법도 막는다:
    `:` (:red[...] 색상, :apple: 이모지, :material/...: 아이콘), `$` (LaTeX 수식).
    """
    return re.sub(r"([\\`*_{}\[\]()#+\-.!~|<>:$])", r"\\\1", text)


# 정적 CSS만 주입한다 (사용자 입력은 절대 포함하지 않음).
# 색상은 고정값 대신 currentColor/투명도/primaryColor를 사용해 라이트·다크 모두 대응.
CUSTOM_CSS = """
<style>
.block-container { max-width: 720px; padding-top: 2.5rem; }

/* 아이템 카드: 좁은 화면에서도 한 줄 유지 (Streamlit 기본은 640px 이하에서 세로로 쌓임) */
[class*="st-key-card_"] [data-testid="stHorizontalBlock"],
.st-key-stats [data-testid="stHorizontalBlock"] { flex-wrap: nowrap; gap: 0.5rem; }
[class*="st-key-card_"] [data-testid="stColumn"],
.st-key-stats [data-testid="stColumn"] { min-width: 0 !important; width: auto !important; }
[class*="st-key-card_"] [data-testid="stMarkdownContainer"] p {
    margin: 0; overflow-wrap: anywhere; font-size: 1.05rem;
}
[class*="st-key-card_"] { padding-top: 0.6rem !important; padding-bottom: 0.6rem !important; }
[class*="st-key-card_"] button { padding: 0.25rem 0.4rem; min-height: 2rem; }

/* 편집 중인 카드 강조 */
[class*="st-key-editing_"] {
    border: 2px solid #16a34a !important;
    background: rgba(22, 163, 74, 0.06);
}

/* 빈 상태 */
.empty-state { text-align: center; padding: 2.5rem 1rem; opacity: 0.75; }
.empty-state .icon { font-size: 3rem; line-height: 1; margin-bottom: 0.75rem; }
.empty-state .title { font-size: 1.1rem; font-weight: 600; margin-bottom: 0.25rem; }
</style>
"""


def render_empty(icon: str, title: str, message: str) -> None:
    st.markdown(
        f'<div class="empty-state"><div class="icon">{icon}</div>'
        f'<div class="title">{title}</div><div>{message}</div></div>',
        unsafe_allow_html=True,  # 고정 문구만 사용
    )


def render_item(item: dict) -> None:
    item_id = item["id"]

    if st.session_state["editing_id"] == item_id:
        with st.container(border=True, key=f"editing_{item_id}"):
            st.caption("✏️ 아이템 수정 중")
            col_name, col_qty = st.columns([3, 1])
            col_name.text_input(
                "이름", key=f"edit_name_{item_id}", max_chars=NAME_MAX_LEN,
                label_visibility="collapsed",
            )
            col_qty.number_input(
                "수량", key=f"edit_qty_{item_id}", min_value=1, step=1,
                label_visibility="collapsed",
            )
            if st.session_state["edit_error"]:
                st.warning(st.session_state["edit_error"], icon="⚠️")
            with st.container(horizontal=True, horizontal_alignment="right"):
                st.button("취소", key=f"cancel_{item_id}", on_click=on_cancel_edit)
                st.button(
                    "저장", key=f"save_{item_id}", type="primary", icon="💾",
                    on_click=on_save_edit, args=(item_id,),
                )
        return

    with st.container(border=True, key=f"card_{item_id}"):
        col_check, col_name, col_qty, col_edit, col_del = st.columns(
            [0.5, 6, 1.1, 0.8, 0.8], vertical_alignment="center"
        )
        # 다른 탭에서 바뀐 체크 상태도 반영되도록 위젯 상태를 파일 값과 동기화한다.
        st.session_state[f"check_{item_id}"] = item["checked"]
        col_check.checkbox(
            "구매 완료", key=f"check_{item_id}",
            on_change=on_toggle, args=(item_id,), label_visibility="collapsed",
        )
        name = escape_markdown(item["name"])
        col_name.markdown(f":gray[~~{name}~~]" if item["checked"] else name)
        badge_color = "gray" if item["checked"] else "green"
        col_qty.markdown(f":{badge_color}-badge[x{item['quantity']}]")
        col_edit.button(
            "✏️", key=f"edit_{item_id}", type="tertiary", help="수정",
            on_click=on_start_edit, args=(item,),
        )
        col_del.button(
            "🗑️", key=f"del_{item_id}", type="tertiary", help="삭제",
            on_click=on_delete, args=(item_id,),
        )


st.set_page_config(page_title="쇼핑 리스트", page_icon="🛒", layout="centered")
st.markdown(CUSTOM_CSS, unsafe_allow_html=True)
init_state()

items = _items()
total = len(items)
done = sum(1 for item in items if item["checked"])

# --- 제목 ---
st.title("🛒 쇼핑 리스트")
st.caption("오늘 살 것들을 정리해 보세요")

# --- 데이터 파일 관련 안내 (손상 복구, 저장 실패 등) ---
if st.session_state["notice"]:
    st.warning(st.session_state["notice"], icon="⚠️")
    st.session_state["notice"] = None

# --- 진행 현황 (F5) ---
with st.container(border=True, key="stats"):
    col_total, col_todo, col_done = st.columns(3)
    col_total.metric("전체", f"{total}개")
    col_todo.metric("남은 아이템", f"{total - done}개")
    col_done.metric("구매 완료", f"{done}개")
    ratio = done / total if total else 0.0
    st.progress(ratio, text=f"전체 {total}개 중 {done}개 구매 완료 ({ratio:.0%})")

# --- 입력 폼 (F1) ---
# clear_on_submit을 쓰지 않는다: 검증 실패 시에도 입력값이 지워지기 때문 (on_add에서 성공 시에만 비움)
with st.form("add_form", border=False):
    col_name, col_qty, col_btn = st.columns([5, 1.6, 1.4], vertical_alignment="bottom")
    form_ver = st.session_state["form_ver"]
    col_name.text_input(
        "아이템 이름", key=f"new_name_{form_ver}", max_chars=NAME_MAX_LEN,
        placeholder="예: 우유",
    )
    col_qty.number_input("수량", key=f"new_qty_{form_ver}", min_value=1, value=1, step=1)
    col_btn.form_submit_button(
        "추가", type="primary", icon="➕", on_click=on_add, width="stretch"
    )
if st.session_state["add_error"]:
    st.warning(st.session_state["add_error"], icon="⚠️")
    st.session_state["add_error"] = None

# --- 보기 필터 (F7) ---
view = st.radio(
    "보기", [FILTER_ALL, FILTER_TODO, FILTER_DONE], key="filter", horizontal=True,
    label_visibility="collapsed",
)

# --- 아이템 목록 (F2 수정, F3 삭제, F4 체크) ---
if view == FILTER_TODO:
    visible = [item for item in items if not item["checked"]]
elif view == FILTER_DONE:
    visible = [item for item in items if item["checked"]]
else:
    visible = items

if not items:
    render_empty("🧺", "아직 추가된 아이템이 없습니다", "위 입력창에서 살 물건을 추가해 보세요!")
elif not visible:
    render_empty("🔍", f"'{view}' 항목이 없습니다", "다른 보기를 선택해 보세요.")
else:
    for item in visible:
        render_item(item)

# --- 하단 버튼 (F6 구매 완료 항목 일괄 삭제) ---
with st.container(horizontal=True, horizontal_alignment="right"):
    st.button(
        "구매 완료 항목 삭제", icon="🧹", disabled=done == 0,
        on_click=on_clear_checked,
    )
