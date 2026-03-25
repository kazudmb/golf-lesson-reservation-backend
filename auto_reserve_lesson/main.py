import base64
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import boto3
import jpholiday
import requests
from bs4 import BeautifulSoup, Tag
from botocore.exceptions import BotoCoreError, ClientError
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

LOCAL_TZ = ZoneInfo("Asia/Tokyo")
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
DEFAULT_CREDENTIALS_SECRET_ID = "auto-reserve-lesson/credentials"

DATE_REGEX = re.compile(
    r"(?:(?P<y>\d{4})[/.年-])?(?P<m>\d{1,2})[/.月-](?P<d>\d{1,2})日?"
)
TIME_REGEX = re.compile(r"(?P<h>[01]?\d|2[0-3]):(?P<m>[0-5]\d)")


class ReservationAutomationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    site_url: str
    member_id: str
    password: str
    seat_label: str
    min_slot_time: time
    polling_start_hour: int
    polling_end_hour: int
    request_timeout_seconds: int
    dry_run: bool
    google_calendar_id: str | None
    google_service_account_json: str | None


@dataclass(frozen=True)
class HtmlPage:
    url: str
    soup: BeautifulSoup


@dataclass(frozen=True)
class NavigationAction:
    method: str
    url: str
    payload: dict[str, str]
    description: str


@dataclass(frozen=True)
class ReservationEntry:
    reserved_date: date | None
    reserved_time: time | None
    label: str
    cancel_action: NavigationAction | None


@dataclass(frozen=True)
class ReservationCandidate:
    reserved_date: date
    reserved_time: time
    available_count: int
    action: NavigationAction


def _response(status_code: int, body: dict[str, Any]):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body, ensure_ascii=False),
    }


def _parse_bool(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_hhmm(value: str, *, name: str) -> time:
    try:
        parsed = datetime.strptime(value, "%H:%M")
    except ValueError as exc:
        raise ReservationAutomationError(f"{name} must be HH:MM format") from exc
    return parsed.time()


def _load_secret_credentials(secret_id: str) -> tuple[str, str]:
    client = boto3.client("secretsmanager")

    try:
        response = client.get_secret_value(SecretId=secret_id)
    except (BotoCoreError, ClientError) as exc:
        raise ReservationAutomationError(
            f"Failed to load lesson credentials from Secrets Manager: {secret_id}"
        ) from exc

    secret_string = response.get("SecretString")
    if secret_string:
        raw_secret = secret_string
    else:
        secret_binary = response.get("SecretBinary")
        if not secret_binary:
            raise ReservationAutomationError(
                f"Secrets Manager secret is empty: {secret_id}"
            )
        try:
            raw_secret = base64.b64decode(secret_binary).decode("utf-8")
        except Exception as exc:
            raise ReservationAutomationError(
                f"Secrets Manager secret could not be decoded: {secret_id}"
            ) from exc

    try:
        secret_payload = json.loads(raw_secret)
    except json.JSONDecodeError as exc:
        raise ReservationAutomationError(
            f"Secrets Manager secret must be valid JSON: {secret_id}"
        ) from exc

    member_id = str(secret_payload.get("LESSON_MEMBER_ID") or "").strip()
    password = str(secret_payload.get("LESSON_PASSWORD") or "").strip()
    if not member_id:
        raise ReservationAutomationError(
            f"LESSON_MEMBER_ID is missing in Secrets Manager secret: {secret_id}"
        )
    if not password:
        raise ReservationAutomationError(
            f"LESSON_PASSWORD is missing in Secrets Manager secret: {secret_id}"
        )

    return member_id, password


def _load_settings(event: dict[str, Any] | None = None) -> Settings:
    payload = event if isinstance(event, dict) else {}

    site_url = str(payload.get("siteUrl") or os.getenv("LESSON_SITE_URL") or "").strip()
    if not site_url:
        site_url = "https://www.spoon3.jp/reserve/index.php?_action=index&site=smart&s=380"

    member_id = str(payload.get("memberId") or "").strip()
    password = str(payload.get("password") or "").strip()
    if not member_id or not password:
        credentials_secret_id = str(
            payload.get("credentialsSecretId")
            or os.getenv("LESSON_CREDENTIALS_SECRET_ID")
            or DEFAULT_CREDENTIALS_SECRET_ID
        ).strip()
        member_id, password = _load_secret_credentials(credentials_secret_id)

    seat_label = str(
        payload.get("seatLabel") or os.getenv("LESSON_SEAT_LABEL") or "ジートラック打席"
    ).strip()
    min_slot_time = _parse_hhmm(
        str(payload.get("minSlotTime") or os.getenv("LESSON_MIN_SLOT_TIME") or "18:40"),
        name="LESSON_MIN_SLOT_TIME",
    )

    polling_start_hour = int(
        str(payload.get("pollingStartHour") or os.getenv("LESSON_POLLING_START_HOUR") or "0")
    )
    polling_end_hour = int(
        str(payload.get("pollingEndHour") or os.getenv("LESSON_POLLING_END_HOUR") or "18")
    )
    if polling_start_hour < 0 or polling_start_hour > 23:
        raise ReservationAutomationError("LESSON_POLLING_START_HOUR must be between 0 and 23")
    if polling_end_hour < 0 or polling_end_hour > 23:
        raise ReservationAutomationError("LESSON_POLLING_END_HOUR must be between 0 and 23")
    if polling_start_hour > polling_end_hour:
        raise ReservationAutomationError(
            "LESSON_POLLING_START_HOUR must be less than or equal to LESSON_POLLING_END_HOUR"
        )

    request_timeout_seconds = int(
        str(payload.get("requestTimeoutSeconds") or os.getenv("LESSON_REQUEST_TIMEOUT_SECONDS") or "20")
    )
    if request_timeout_seconds <= 0:
        raise ReservationAutomationError("LESSON_REQUEST_TIMEOUT_SECONDS must be positive")

    dry_run = _parse_bool(
        str(payload.get("dryRun") or os.getenv("LESSON_DRY_RUN") or "false"),
        default=False,
    )

    google_calendar_id = str(
        payload.get("googleCalendarId") or os.getenv("GOOGLE_CALENDAR_ID") or ""
    ).strip() or None
    google_service_account_json = str(
        payload.get("googleServiceAccountJson") or os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") or ""
    ).strip() or None

    return Settings(
        site_url=site_url,
        member_id=member_id,
        password=password,
        seat_label=seat_label,
        min_slot_time=min_slot_time,
        polling_start_hour=polling_start_hour,
        polling_end_hour=polling_end_hour,
        request_timeout_seconds=request_timeout_seconds,
        dry_run=dry_run,
        google_calendar_id=google_calendar_id,
        google_service_account_json=google_service_account_json,
    )


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _extract_date(value: str, *, base_date: date) -> date | None:
    for match in DATE_REGEX.finditer(value):
        month = int(match.group("m"))
        day = int(match.group("d"))
        if month < 1 or month > 12 or day < 1 or day > 31:
            continue
        year_str = match.group("y")
        year = int(year_str) if year_str else base_date.year
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if not year_str and candidate < base_date - timedelta(days=120):
            candidate = date(base_date.year + 1, month, day)
        return candidate
    return None


def _extract_time(value: str) -> time | None:
    match = TIME_REGEX.search(value)
    if not match:
        return None
    return time(int(match.group("h")), int(match.group("m")))


def _time_is_in_window(now: datetime, settings: Settings) -> bool:
    return settings.polling_start_hour <= now.hour <= settings.polling_end_hour


def _can_reserve_on(target: date) -> bool:
    if jpholiday.is_holiday(target):
        return True
    return target.weekday() < 5


def _parse_service_account_info(raw_value: str) -> dict[str, Any]:
    try:
        return json.loads(raw_value)
    except json.JSONDecodeError:
        pass

    try:
        decoded = base64.b64decode(raw_value).decode("utf-8")
        return json.loads(decoded)
    except Exception as exc:
        raise ReservationAutomationError(
            "GOOGLE_SERVICE_ACCOUNT_JSON must be raw JSON or base64 encoded JSON"
        ) from exc


def _has_google_calendar_conflict(settings: Settings, target_day: date) -> bool:
    if not settings.google_calendar_id or not settings.google_service_account_json:
        logger.info("Google Calendar config is missing, skipping calendar check")
        return False

    credentials = service_account.Credentials.from_service_account_info(
        _parse_service_account_info(settings.google_service_account_json), scopes=[CALENDAR_SCOPE]
    )

    start_dt = datetime.combine(target_day, time(0, 0), tzinfo=LOCAL_TZ)
    end_dt = start_dt + timedelta(days=1)

    try:
        service = build("calendar", "v3", credentials=credentials, cache_discovery=False)
        response = (
            service.events()
            .list(
                calendarId=settings.google_calendar_id,
                timeMin=start_dt.isoformat(),
                timeMax=end_dt.isoformat(),
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
    except HttpError as exc:
        raise ReservationAutomationError(f"Google Calendar API request failed: {exc}") from exc

    for event in response.get("items", []):
        if event.get("status") == "cancelled":
            continue
        logger.info("Calendar conflict found: %s", event.get("summary", "(no summary)"))
        return True
    return False


class ReservationClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) lesson-auto-reserver/1.0",
                "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
            }
        )

    def open(self, url: str) -> HtmlPage:
        response = self.session.get(url, timeout=self.settings.request_timeout_seconds)
        response.raise_for_status()
        return HtmlPage(url=response.url, soup=BeautifulSoup(response.text, "html.parser"))

    def request(self, action: NavigationAction) -> HtmlPage:
        method = action.method.lower()
        if method == "get":
            response = self.session.get(
                action.url,
                params=action.payload,
                timeout=self.settings.request_timeout_seconds,
            )
        else:
            response = self.session.post(
                action.url,
                data=action.payload,
                timeout=self.settings.request_timeout_seconds,
            )
        response.raise_for_status()
        return HtmlPage(url=response.url, soup=BeautifulSoup(response.text, "html.parser"))

    def _extract_form_payload(self, form: Tag) -> dict[str, str]:
        payload: dict[str, str] = {}

        for input_tag in form.find_all("input"):
            if not isinstance(input_tag, Tag):
                continue
            if input_tag.has_attr("disabled"):
                continue
            name = (input_tag.get("name") or "").strip()
            if not name:
                continue

            input_type = str(input_tag.get("type") or "text").lower()
            if input_type in {"checkbox", "radio"} and not input_tag.has_attr("checked"):
                continue

            payload[name] = str(input_tag.get("value") or "")

        for textarea_tag in form.find_all("textarea"):
            if not isinstance(textarea_tag, Tag):
                continue
            if textarea_tag.has_attr("disabled"):
                continue
            name = (textarea_tag.get("name") or "").strip()
            if not name:
                continue
            payload[name] = textarea_tag.text or ""

        for select_tag in form.find_all("select"):
            if not isinstance(select_tag, Tag):
                continue
            if select_tag.has_attr("disabled"):
                continue
            name = (select_tag.get("name") or "").strip()
            if not name:
                continue
            options = select_tag.find_all("option")
            selected = next((opt for opt in options if opt.has_attr("selected")), None)
            if selected is None and options:
                selected = options[0]
            payload[name] = str((selected.get("value") if selected else "") or "")

        return payload

    def _form_action(self, page: HtmlPage, form: Tag) -> tuple[str, str]:
        action = str(form.get("action") or "").strip()
        url = urljoin(page.url, action) if action else page.url
        method = str(form.get("method") or "post").lower()
        return url, method

    def _form_text(self, form: Tag) -> str:
        return _clean_text(form.get_text(" ", strip=True))

    def _find_submit_control(self, form: Tag, labels: list[str]) -> Tag | None:
        candidates: list[Tag] = []
        for tag in form.find_all(["button", "input"]):
            if not isinstance(tag, Tag):
                continue
            if tag.name == "input":
                input_type = str(tag.get("type") or "").lower()
                if input_type not in {"submit", "button", "image"}:
                    continue
                text = str(tag.get("value") or "")
            else:
                button_type = str(tag.get("type") or "submit").lower()
                if button_type not in {"submit", "button"}:
                    continue
                text = tag.get_text(" ", strip=True)

            normalized = _clean_text(text)
            if not normalized:
                continue
            candidates.append(tag)
            if any(label in normalized for label in labels):
                return tag
        return candidates[0] if candidates else None

    def _submit_form(
        self,
        page: HtmlPage,
        form: Tag,
        *,
        updates: dict[str, str] | None = None,
        submit_control: Tag | None = None,
    ) -> HtmlPage:
        payload = self._extract_form_payload(form)
        if updates:
            payload.update(updates)

        if submit_control is not None:
            control_name = str(submit_control.get("name") or "").strip()
            if control_name:
                payload[control_name] = str(submit_control.get("value") or "")

        action_url, method = self._form_action(page, form)
        nav_action = NavigationAction(
            method=method,
            url=action_url,
            payload=payload,
            description="form submission",
        )
        return self.request(nav_action)

    def _find_form_with_password(self, page: HtmlPage) -> Tag | None:
        for form in page.soup.find_all("form"):
            if form.find("input", {"type": "password"}):
                return form
        return None

    def _find_member_form(self, page: HtmlPage) -> Tag | None:
        for form in page.soup.find_all("form"):
            if form.find("input", {"type": "password"}):
                continue

            for input_tag in form.find_all("input"):
                if not isinstance(input_tag, Tag):
                    continue
                if input_tag.has_attr("disabled"):
                    continue

                input_type = str(input_tag.get("type") or "text").lower()
                if input_type not in {"text", "tel", "number"}:
                    continue

                name = str(input_tag.get("name") or "").lower()
                placeholder = str(input_tag.get("placeholder") or "")
                if any(key in name for key in ["member", "kaiin", "id", "no"]):
                    return form
                if "会員" in placeholder:
                    return form

            form_text = self._form_text(form)
            if "会員" in form_text and "番号" in form_text:
                return form

        return None

    def _field_name(self, form: Tag, *, password: bool = False) -> str:
        for input_tag in form.find_all("input"):
            if not isinstance(input_tag, Tag):
                continue
            if input_tag.has_attr("disabled"):
                continue

            input_type = str(input_tag.get("type") or "text").lower()
            if password and input_type == "password":
                name = str(input_tag.get("name") or "").strip()
                if name:
                    return name
            if not password and input_type in {"text", "tel", "number"}:
                name = str(input_tag.get("name") or "").strip()
                if name:
                    return name

        field_type = "password" if password else "member"
        raise ReservationAutomationError(f"{field_type} field name not found")

    def ensure_authenticated(self, page: HtmlPage) -> HtmlPage:
        current = page
        for _ in range(4):
            member_form = self._find_member_form(current)
            if member_form is not None:
                member_field = self._field_name(member_form, password=False)
                submit = self._find_submit_control(member_form, ["次へ", "進む", "ログイン"])
                current = self._submit_form(
                    current,
                    member_form,
                    updates={member_field: self.settings.member_id},
                    submit_control=submit,
                )
                continue

            password_form = self._find_form_with_password(current)
            if password_form is not None:
                password_field = self._field_name(password_form, password=True)
                submit = self._find_submit_control(password_form, ["次へ", "進む", "ログイン"])
                current = self._submit_form(
                    current,
                    password_form,
                    updates={password_field: self.settings.password},
                    submit_control=submit,
                )
                continue

            break

        return current

    def _follow_link_by_text(
        self,
        page: HtmlPage,
        labels: list[str],
        *,
        exclude_labels: list[str] | None = None,
    ) -> HtmlPage | None:
        excludes = exclude_labels or []
        for link in page.soup.find_all("a"):
            if not isinstance(link, Tag):
                continue
            text = _clean_text(link.get_text(" ", strip=True))
            if not text:
                continue
            if not any(label in text for label in labels):
                continue
            if any(exclude in text for exclude in excludes):
                continue
            href = str(link.get("href") or "").strip()
            if not href:
                continue
            if href.startswith("javascript"):
                continue
            target = urljoin(page.url, href)
            return self.open(target)
        return None

    def _submit_form_by_text(
        self,
        page: HtmlPage,
        labels: list[str],
        *,
        exclude_labels: list[str] | None = None,
    ) -> HtmlPage | None:
        excludes = exclude_labels or []
        for form in page.soup.find_all("form"):
            if not isinstance(form, Tag):
                continue
            form_text = self._form_text(form)
            if not any(label in form_text for label in labels):
                continue
            if any(exclude in form_text for exclude in excludes):
                continue
            submit = self._find_submit_control(form, labels)
            return self._submit_form(page, form, submit_control=submit)
        return None

    def open_reservation_list(self, page: HtmlPage) -> HtmlPage:
        linked = self._follow_link_by_text(page, ["予約確認", "予約状況", "予約一覧", "確認・変更"])
        if linked is not None:
            return linked

        submitted = self._submit_form_by_text(page, ["予約確認", "予約状況", "予約一覧", "確認・変更"])
        if submitted is not None:
            return submitted

        return page

    def open_booking_page(self, page: HtmlPage) -> HtmlPage:
        linked = self._follow_link_by_text(
            page,
            ["レッスン", "予約"],
            exclude_labels=["確認", "状況", "一覧", "変更", "キャンセル"],
        )
        if linked is not None:
            return linked

        submitted = self._submit_form_by_text(
            page,
            ["レッスン", "予約"],
            exclude_labels=["確認", "状況", "一覧", "変更", "キャンセル"],
        )
        if submitted is not None:
            return submitted

        raise ReservationAutomationError("Booking entry action was not found")

    def _find_select_for_seat(self, page: HtmlPage) -> tuple[Tag, Tag, str] | None:
        for form in page.soup.find_all("form"):
            if not isinstance(form, Tag):
                continue
            for select in form.find_all("select"):
                if not isinstance(select, Tag):
                    continue
                select_name = str(select.get("name") or "").strip()
                if not select_name:
                    continue

                for option in select.find_all("option"):
                    if not isinstance(option, Tag):
                        continue
                    option_text = _clean_text(option.get_text(" ", strip=True))
                    if self.settings.seat_label in option_text:
                        option_value = str(option.get("value") or "").strip()
                        return form, select, option_value
        return None

    def select_seat_if_needed(self, page: HtmlPage) -> HtmlPage:
        seat_target = self._find_select_for_seat(page)
        if seat_target is None:
            seat_select_exists = False
            for select in page.soup.find_all("select"):
                if not isinstance(select, Tag):
                    continue
                if "打席" in _clean_text(select.get_text(" ", strip=True)):
                    seat_select_exists = True
                    break
            if seat_select_exists:
                raise ReservationAutomationError(
                    f"Seat option '{self.settings.seat_label}' was not found"
                )
            return page

        form, select_tag, option_value = seat_target
        updates = {str(select_tag.get("name")): option_value}
        submit = self._find_submit_control(form, ["次へ", "進む", "決定", "検索", "表示"])
        return self._submit_form(page, form, updates=updates, submit_control=submit)

    def _action_from_control(self, page: HtmlPage, control: Tag) -> NavigationAction | None:
        if control.has_attr("disabled"):
            return None

        classes = {str(cls).lower() for cls in control.get("class", []) if cls}
        if "disabled" in classes:
            return None

        if control.name == "a":
            href = str(control.get("href") or "").strip()
            if not href or href.startswith("javascript"):
                return None
            return NavigationAction(
                method="get",
                url=urljoin(page.url, href),
                payload={},
                description="availability link click",
            )

        form = control.find_parent("form")
        if not isinstance(form, Tag):
            return None

        payload = self._extract_form_payload(form)
        control_name = str(control.get("name") or "").strip()
        if control_name:
            payload[control_name] = str(control.get("value") or "")

        action_url, method = self._form_action(page, form)
        return NavigationAction(
            method=method,
            url=action_url,
            payload=payload,
            description="availability form click",
        )

    def _parse_candidates_from_row(
        self,
        page: HtmlPage,
        row: Tag,
        *,
        target_day: date,
        base_date: date,
    ) -> list[ReservationCandidate]:
        row_text = _clean_text(row.get_text(" ", strip=True))
        slot_time = _extract_time(row_text)
        if slot_time is None:
            return []
        if slot_time < self.settings.min_slot_time:
            return []

        parsed_date = _extract_date(row_text, base_date=base_date) or target_day
        if parsed_date != target_day:
            return []

        candidates: list[ReservationCandidate] = []
        for control in row.find_all(["a", "button", "input"]):
            if not isinstance(control, Tag):
                continue

            label = ""
            if control.name == "input":
                input_type = str(control.get("type") or "").lower()
                if input_type not in {"submit", "button", "image"}:
                    continue
                label = str(control.get("value") or "")
            else:
                label = control.get_text(" ", strip=True)

            label = _clean_text(label)
            if not label or not label.isdigit():
                continue

            available_count = int(label)
            if available_count < 1:
                continue

            action = self._action_from_control(page, control)
            if action is None:
                continue

            candidates.append(
                ReservationCandidate(
                    reserved_date=parsed_date,
                    reserved_time=slot_time,
                    available_count=available_count,
                    action=action,
                )
            )

        return candidates

    def find_available_slots(self, page: HtmlPage, *, target_day: date) -> list[ReservationCandidate]:
        base_date = target_day
        heading = page.soup.find(["h1", "h2", "h3", "title"])
        if isinstance(heading, Tag):
            heading_date = _extract_date(_clean_text(heading.get_text(" ", strip=True)), base_date=target_day)
            if heading_date:
                base_date = heading_date

        candidates: list[ReservationCandidate] = []
        for row in page.soup.find_all("tr"):
            if not isinstance(row, Tag):
                continue
            candidates.extend(
                self._parse_candidates_from_row(
                    page,
                    row,
                    target_day=target_day,
                    base_date=base_date,
                )
            )

        candidates.sort(key=lambda item: item.reserved_time)
        return candidates

    def _extract_cancel_action(self, page: HtmlPage, form: Tag) -> NavigationAction | None:
        submit = self._find_submit_control(form, ["キャンセル", "取消"])
        if submit is None:
            return None

        submit_text = _clean_text(submit.get_text(" ", strip=True) if submit.name == "button" else str(submit.get("value") or ""))
        if "キャンセル" not in submit_text and "取消" not in submit_text:
            return None

        payload = self._extract_form_payload(form)
        submit_name = str(submit.get("name") or "").strip()
        if submit_name:
            payload[submit_name] = str(submit.get("value") or "")

        action_url, method = self._form_action(page, form)
        return NavigationAction(
            method=method,
            url=action_url,
            payload=payload,
            description="cancel reservation",
        )

    def get_existing_reservations(self, page: HtmlPage, *, base_date: date) -> list[ReservationEntry]:
        entries: list[ReservationEntry] = []
        seen: set[tuple[date | None, time | None, str]] = set()

        for form in page.soup.find_all("form"):
            if not isinstance(form, Tag):
                continue
            form_text = self._form_text(form)
            if "予約" not in form_text and "キャンセル" not in form_text:
                continue

            reserved_date = _extract_date(form_text, base_date=base_date)
            reserved_time = _extract_time(form_text)
            cancel_action = self._extract_cancel_action(page, form)
            label = form_text[:120]
            key = (reserved_date, reserved_time, label)
            if key in seen:
                continue
            seen.add(key)
            entries.append(
                ReservationEntry(
                    reserved_date=reserved_date,
                    reserved_time=reserved_time,
                    label=label,
                    cancel_action=cancel_action,
                )
            )

        for node in page.soup.find_all(string=re.compile("予約")):
            parent = node.parent
            if not isinstance(parent, Tag):
                continue
            text = _clean_text(parent.get_text(" ", strip=True))
            if not text:
                continue

            reserved_date = _extract_date(text, base_date=base_date)
            reserved_time = _extract_time(text)
            if reserved_date is None and reserved_time is None:
                continue

            label = text[:120]
            key = (reserved_date, reserved_time, label)
            if key in seen:
                continue
            seen.add(key)
            entries.append(
                ReservationEntry(
                    reserved_date=reserved_date,
                    reserved_time=reserved_time,
                    label=label,
                    cancel_action=None,
                )
            )

        return entries

    def finalize_reservation(self, page: HtmlPage) -> HtmlPage:
        for form in page.soup.find_all("form"):
            if not isinstance(form, Tag):
                continue
            submit = self._find_submit_control(form, ["予約する", "予約確定", "この内容で予約"])
            if submit is None:
                continue

            submit_text = _clean_text(
                submit.get_text(" ", strip=True)
                if submit.name == "button"
                else str(submit.get("value") or "")
            )
            if "予約" not in submit_text:
                continue

            return self._submit_form(page, form, submit_control=submit)

        return page


class ReservationAutomation:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = ReservationClient(settings)

    def run(self, *, now: datetime) -> dict[str, Any]:
        now_local = now.astimezone(LOCAL_TZ)
        today = now_local.date()

        if not _time_is_in_window(now_local, self.settings):
            return {
                "status": "skipped",
                "reason": "outside_polling_window",
                "now": now_local.isoformat(),
            }

        if not _can_reserve_on(today):
            return {
                "status": "skipped",
                "reason": "weekend_non_holiday",
                "date": today.isoformat(),
            }

        if _has_google_calendar_conflict(self.settings, today):
            return {
                "status": "skipped",
                "reason": "google_calendar_conflict",
                "date": today.isoformat(),
            }

        home = self.client.open(self.settings.site_url)
        home = self.client.ensure_authenticated(home)

        reservation_page = self.client.open_reservation_list(home)
        reservation_page = self.client.ensure_authenticated(reservation_page)

        existing_reservations = self.client.get_existing_reservations(reservation_page, base_date=today)

        if any(entry.reserved_date == today for entry in existing_reservations):
            return {
                "status": "skipped",
                "reason": "already_reserved_today",
                "date": today.isoformat(),
            }

        booking_page = self.client.open_booking_page(home)
        booking_page = self.client.ensure_authenticated(booking_page)
        booking_page = self.client.select_seat_if_needed(booking_page)

        available_slots = self.client.find_available_slots(booking_page, target_day=today)
        if not available_slots:
            return {
                "status": "skipped",
                "reason": "no_available_slot",
                "date": today.isoformat(),
                "minSlotTime": self.settings.min_slot_time.strftime("%H:%M"),
            }

        chosen_slot = available_slots[0]

        cancellations: list[str] = []
        future_reservations = [
            entry
            for entry in existing_reservations
            if entry.reserved_date is not None and entry.reserved_date > today
        ]
        non_cancellable_future = [
            entry for entry in future_reservations if entry.cancel_action is None
        ]

        if self.settings.dry_run:
            return {
                "status": "dry_run",
                "date": today.isoformat(),
                "chosenSlot": chosen_slot.reserved_time.strftime("%H:%M"),
                "availableCount": chosen_slot.available_count,
                "futureReservationCount": len(future_reservations),
            }

        if non_cancellable_future:
            return {
                "status": "skipped",
                "reason": "future_reservation_without_cancel_action",
                "date": today.isoformat(),
                "futureReservationCount": len(future_reservations),
            }

        for future in future_reservations:
            if future.cancel_action is None:
                logger.warning("Skipping cancellation due to missing action: %s", future.label)
                continue
            self.client.request(future.cancel_action)
            cancellations.append(future.label)

        confirm_page = self.client.request(chosen_slot.action)
        completed_page = self.client.finalize_reservation(confirm_page)

        completed_text = _clean_text(completed_page.soup.get_text(" ", strip=True))
        is_completed = any(keyword in completed_text for keyword in ["予約完了", "予約を受け付けました", "予約が完了"])

        return {
            "status": "reserved" if is_completed else "submitted",
            "date": today.isoformat(),
            "slot": chosen_slot.reserved_time.strftime("%H:%M"),
            "availableCount": chosen_slot.available_count,
            "cancelledReservations": cancellations,
            "confirmationDetected": is_completed,
        }


def handler(event, context):
    logger.info("Received event: %s", json.dumps(event, ensure_ascii=False, default=str))
    try:
        settings = _load_settings(event if isinstance(event, dict) else None)
        automation = ReservationAutomation(settings)
        result = automation.run(now=datetime.now(LOCAL_TZ))
        return _response(200, result)
    except ReservationAutomationError as exc:
        logger.warning("Automation validation failed: %s", exc)
        return _response(400, {"status": "error", "message": str(exc)})
    except requests.RequestException as exc:
        logger.error("Reservation site request failed: %s", exc, exc_info=True)
        return _response(502, {"status": "error", "message": "Reservation site request failed"})
    except Exception as exc:
        logger.error("Unexpected error: %s", exc, exc_info=True)
        return _response(500, {"status": "error", "message": "Internal server error"})
