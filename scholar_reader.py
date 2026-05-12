from __future__ import annotations

import argparse
import json
import os
import queue
import re
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

if TYPE_CHECKING:
    import tkinter as tk


DEFAULT_USER_ID = "JbrDaPAAAAAJ"
DEFAULT_CACHE_DAYS = 7
DEFAULT_PROFILE_FILE = Path(__file__).resolve().parent / "scholar_profile.json"
SCHEMA_VERSION = 2
SCHOLAR_URL = "https://scholar.google.com/citations"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)


@dataclass
class Article:
    title: str
    citations: int
    year: int | None


@dataclass
class ScholarProfile:
    schema_version: int
    user_id: str
    profile_url: str
    name: str
    citations: int
    h_index: int
    i10_index: int
    articles: list[Article]
    date: str
    retrieved_at: str


def profile_url(user_id: str) -> str:
    query = urlencode({"user": user_id, "hl": "en", "pagesize": 100})
    return f"{SCHOLAR_URL}?{query}"


def parse_int(text: str, default: int = 0) -> int:
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else default


def parse_year(text: str) -> int | None:
    match = re.search(r"\b(18|19|20)\d{2}\b", text)
    return int(match.group(0)) if match else None


def clean_text(parts: list[str]) -> str:
    return " ".join("".join(parts).split())


class ScholarProfileParser(HTMLParser):
    """Small parser for the public Google Scholar profile page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.name = ""
        self.metrics: dict[str, int] = {}
        self.articles: list[Article] = []

        self._capture_name = False
        self._name_parts: list[str] = []

        self._in_tr = False
        self._capture_metric_label = False
        self._capture_metric_value = False
        self._metric_label_parts: list[str] = []
        self._metric_value_parts: list[str] = []
        self._metric_label = ""
        self._metric_values: list[int] = []

        self._in_article = False
        self._capture_title = False
        self._capture_citations = False
        self._capture_year = False
        self._article_title_parts: list[str] = []
        self._article_citation_parts: list[str] = []
        self._article_year_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {name: value or "" for name, value in attrs}
        classes = set(attr.get("class", "").split())

        if tag == "div" and attr.get("id") == "gsc_prf_in":
            self._capture_name = True
            self._name_parts = []

        if tag == "tr":
            self._in_tr = True
            self._metric_label = ""
            self._metric_values = []
            self._in_article = "gsc_a_tr" in classes
            if self._in_article:
                self._article_title_parts = []
                self._article_citation_parts = []
                self._article_year_parts = []

        if self._in_tr and tag == "td" and "gsc_rsb_sc1" in classes:
            self._capture_metric_label = True
            self._metric_label_parts = []

        if self._in_tr and tag == "td" and "gsc_rsb_std" in classes:
            self._capture_metric_value = True
            self._metric_value_parts = []

        if self._in_article and tag == "a" and "gsc_a_at" in classes:
            self._capture_title = True
            self._article_title_parts = []

        if self._in_article and tag == "a" and "gsc_a_ac" in classes:
            self._capture_citations = True
            self._article_citation_parts = []

        if self._in_article and tag == "span" and "gsc_a_h" in classes:
            self._capture_year = True
            self._article_year_parts = []

    def handle_data(self, data: str) -> None:
        if self._capture_name:
            self._name_parts.append(data)
        if self._capture_metric_label:
            self._metric_label_parts.append(data)
        if self._capture_metric_value:
            self._metric_value_parts.append(data)
        if self._capture_title:
            self._article_title_parts.append(data)
        if self._capture_citations:
            self._article_citation_parts.append(data)
        if self._capture_year:
            self._article_year_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "div" and self._capture_name:
            self.name = clean_text(self._name_parts)
            self._capture_name = False

        if tag == "td" and self._capture_metric_label:
            self._metric_label = clean_text(self._metric_label_parts)
            self._capture_metric_label = False

        if tag == "td" and self._capture_metric_value:
            self._metric_values.append(parse_int(clean_text(self._metric_value_parts)))
            self._capture_metric_value = False

        if tag == "a" and self._capture_title:
            self._capture_title = False

        if tag == "a" and self._capture_citations:
            self._capture_citations = False

        if tag == "span" and self._capture_year:
            self._capture_year = False

        if tag == "tr" and self._in_tr:
            self._finish_metric_row()
            self._finish_article_row()
            self._in_tr = False
            self._in_article = False

    def _finish_metric_row(self) -> None:
        if not self._metric_label or not self._metric_values:
            return

        key = re.sub(r"[^a-z0-9]", "", self._metric_label.lower())
        first_value = self._metric_values[0]
        if "citation" in key:
            self.metrics["citations"] = first_value
        elif key == "hindex":
            self.metrics["h_index"] = first_value
        elif key == "i10index":
            self.metrics["i10_index"] = first_value

    def _finish_article_row(self) -> None:
        if not self._in_article:
            return

        title = clean_text(self._article_title_parts)
        if not title:
            return

        citations = parse_int(clean_text(self._article_citation_parts))
        year = parse_year(clean_text(self._article_year_parts))
        self.articles.append(Article(title=title, citations=citations, year=year))


def fetch_profile_html(user_id: str, timeout: int = 20) -> str:
    url = profile_url(user_id)
    request = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(request, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace")
    except HTTPError as exc:
        raise RuntimeError(f"Google Scholar returned HTTP {exc.code}.") from exc
    except URLError as exc:
        raise RuntimeError(f"Could not reach Google Scholar: {exc.reason}") from exc


def parse_profile_html(user_id: str, html: str) -> ScholarProfile:
    lowered = html.lower()
    if "unusual traffic" in lowered or "recaptcha" in lowered:
        raise RuntimeError(
            "Google Scholar returned a bot check. Try again later or use the cached data."
        )

    parser = ScholarProfileParser()
    parser.feed(html)

    if not parser.name:
        raise RuntimeError("Could not find a Google Scholar profile name in the page.")

    now = datetime.now().astimezone()
    articles = sorted(parser.articles, key=lambda item: item.citations, reverse=True)[:10]
    return ScholarProfile(
        schema_version=SCHEMA_VERSION,
        user_id=user_id,
        profile_url=profile_url(user_id),
        name=parser.name,
        citations=parser.metrics.get("citations", 0),
        h_index=parser.metrics.get("h_index", 0),
        i10_index=parser.metrics.get("i10_index", 0),
        articles=articles,
        date=now.date().isoformat(),
        retrieved_at=now.isoformat(timespec="seconds"),
    )


def fetch_profile(user_id: str) -> ScholarProfile:
    return parse_profile_html(user_id, fetch_profile_html(user_id))


def profile_to_dict(profile: ScholarProfile) -> dict[str, Any]:
    data = asdict(profile)
    data["articles"] = [asdict(article) for article in profile.articles]
    return data


def profile_from_dict(data: dict[str, Any]) -> ScholarProfile:
    articles = [Article(**article) for article in data.get("articles", [])]
    return ScholarProfile(
        schema_version=int(data.get("schema_version", 0)),
        user_id=data.get("user_id", ""),
        profile_url=data.get("profile_url", ""),
        name=data.get("name", ""),
        citations=int(data.get("citations", 0)),
        h_index=int(data.get("h_index", 0)),
        i10_index=int(data.get("i10_index", 0)),
        articles=articles,
        date=data.get("date", ""),
        retrieved_at=data.get("retrieved_at", data.get("date", "")),
    )


def save_profile(
    profile: ScholarProfile, profile_file: Path = DEFAULT_PROFILE_FILE
) -> Path:
    profile_file = Path(profile_file)
    profile_file.parent.mkdir(parents=True, exist_ok=True)
    profile_file.write_text(
        json.dumps(profile_to_dict(profile), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return profile_file


def load_cached_profile(
    user_id: str, profile_file: Path = DEFAULT_PROFILE_FILE
) -> ScholarProfile | None:
    profile_file = Path(profile_file)
    if not profile_file.exists():
        return None

    data = json.loads(profile_file.read_text(encoding="utf-8"))
    profile = profile_from_dict(data)
    if profile.user_id != user_id:
        return None
    return profile


def parse_cached_datetime(profile: ScholarProfile) -> datetime | None:
    for raw in (profile.retrieved_at, profile.date):
        if not raw:
            continue
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        return parsed
    return None


def is_cache_fresh(profile: ScholarProfile, max_age_days: float) -> bool:
    if max_age_days < 0:
        return False

    cached_at = parse_cached_datetime(profile)
    if not cached_at:
        return False

    return datetime.now().astimezone() - cached_at <= timedelta(days=max_age_days)


def get_profile(
    user_id: str,
    max_age_days: float = DEFAULT_CACHE_DAYS,
    profile_file: Path = DEFAULT_PROFILE_FILE,
) -> tuple[ScholarProfile, str]:
    cached = load_cached_profile(user_id, profile_file)
    if cached and is_cache_fresh(cached, max_age_days):
        return cached, "Loaded fresh cached data."

    try:
        profile = fetch_profile(user_id)
        saved_file = save_profile(profile, profile_file)
        return profile, f"Retrieved from Google Scholar and saved to {saved_file}."
    except Exception as exc:
        if cached:
            return cached, f"Using stale cached data because refresh failed: {exc}"
        raise


class ScholarReaderApp:
    def __init__(
        self,
        root: tk.Tk,
        user_id: str = DEFAULT_USER_ID,
        max_age_days: float = DEFAULT_CACHE_DAYS,
        profile_file: Path = DEFAULT_PROFILE_FILE,
    ) -> None:
        self.root = root
        self.profile_file = profile_file

        self.user_id_var = tk.StringVar(value=user_id)
        self.max_age_var = tk.StringVar(value=str(max_age_days))
        self.status_var = tk.StringVar(value="Ready.")
        self.name_var = tk.StringVar(value="-")
        self.citations_var = tk.StringVar(value="-")
        self.h_index_var = tk.StringVar(value="-")
        self.i10_index_var = tk.StringVar(value="-")
        self.date_var = tk.StringVar(value="-")
        self.url_var = tk.StringVar(value="-")
        self.result_queue: queue.Queue[tuple[str, Any, str]] = queue.Queue()

        self.root.title("Scholar Reader")
        self.root.geometry("880x520")
        self.root.minsize(720, 420)
        self._build_ui()
        self._poll_results()
        self.load_profile()

    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(2, weight=1)

        controls = ttk.Frame(self.root, padding=(12, 12, 12, 6))
        controls.grid(row=0, column=0, sticky="ew")
        controls.columnconfigure(1, weight=1)

        ttk.Label(controls, text="Scholar user ID").grid(row=0, column=0, sticky="w")
        user_entry = ttk.Entry(controls, textvariable=self.user_id_var)
        user_entry.grid(row=0, column=1, sticky="ew", padx=(8, 12))

        ttk.Label(controls, text="Cache days").grid(row=0, column=2, sticky="w")
        max_age = ttk.Spinbox(
            controls,
            from_=0,
            to=365,
            increment=1,
            textvariable=self.max_age_var,
            width=8,
        )
        max_age.grid(row=0, column=3, sticky="w", padx=(8, 12))

        self.load_button = ttk.Button(controls, text="Load", command=self.load_profile)
        self.load_button.grid(row=0, column=4, padx=(0, 8))

        self.force_button = ttk.Button(
            controls,
            text="Force refresh",
            command=lambda: self.load_profile(force=True),
        )
        self.force_button.grid(row=0, column=5)

        summary = ttk.LabelFrame(self.root, text="Profile", padding=12)
        summary.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 8))
        for index in range(8):
            summary.columnconfigure(index, weight=1)

        fields = [
            ("Name", self.name_var),
            ("Citations", self.citations_var),
            ("H-index", self.h_index_var),
            ("i10-index", self.i10_index_var),
            ("Date", self.date_var),
            ("URL", self.url_var),
        ]
        for column, (label, variable) in enumerate(fields):
            frame = ttk.Frame(summary)
            frame.grid(row=0, column=column, sticky="nsew", padx=(0, 10))
            ttk.Label(frame, text=label).grid(row=0, column=0, sticky="w")
            ttk.Label(frame, textvariable=variable, wraplength=210).grid(
                row=1, column=0, sticky="w"
            )

        table_frame = ttk.Frame(self.root, padding=(12, 0, 12, 6))
        table_frame.grid(row=2, column=0, sticky="nsew")
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)

        self.tree = ttk.Treeview(
            table_frame,
            columns=("title", "citations", "year"),
            show="headings",
            height=12,
        )
        self.tree.heading("title", text="Title")
        self.tree.heading("citations", text="Citations")
        self.tree.heading("year", text="Year")
        self.tree.column("title", width=620, minwidth=260, anchor="w")
        self.tree.column("citations", width=100, minwidth=90, anchor="e")
        self.tree.column("year", width=90, minwidth=70, anchor="center")
        self.tree.grid(row=0, column=0, sticky="nsew")

        scrollbar = ttk.Scrollbar(
            table_frame, orient="vertical", command=self.tree.yview
        )
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scrollbar.set)

        status = ttk.Label(
            self.root,
            textvariable=self.status_var,
            anchor="w",
            padding=(12, 4, 12, 8),
        )
        status.grid(row=3, column=0, sticky="ew")

    def load_profile(self, force: bool = False) -> None:
        user_id = self.user_id_var.get().strip()
        if not user_id:
            messagebox.showerror("Missing user ID", "Enter a Google Scholar user ID.")
            return

        try:
            max_age_days = float(self.max_age_var.get())
        except ValueError:
            messagebox.showerror(
                "Invalid cache days", "Cache days must be a number, such as 7."
            )
            return

        if force:
            max_age_days = -1

        self._set_loading(True)
        thread = threading.Thread(
            target=self._load_worker,
            args=(user_id, max_age_days),
            daemon=True,
        )
        thread.start()

    def _load_worker(self, user_id: str, max_age_days: float) -> None:
        try:
            profile, status = get_profile(user_id, max_age_days, self.profile_file)
        except Exception as exc:
            self.result_queue.put(("error", str(exc), ""))
            return

        self.result_queue.put(("profile", profile, status))

    def _poll_results(self) -> None:
        while True:
            try:
                kind, payload, status = self.result_queue.get_nowait()
            except queue.Empty:
                break

            if kind == "error":
                self._show_error(str(payload))
            else:
                self._show_profile(payload, status)

        self.root.after(100, self._poll_results)

    def _set_loading(self, loading: bool) -> None:
        state = "disabled" if loading else "normal"
        self.load_button.configure(state=state)
        self.force_button.configure(state=state)
        self.status_var.set("Loading...")

    def _show_error(self, message: str) -> None:
        self._set_loading(False)
        self.status_var.set(message)
        messagebox.showerror("Scholar Reader", message)

    def _show_profile(self, profile: ScholarProfile, status: str) -> None:
        self._set_loading(False)
        self.name_var.set(profile.name)
        self.citations_var.set(f"{profile.citations:,}")
        self.h_index_var.set(f"{profile.h_index:,}")
        self.i10_index_var.set(f"{profile.i10_index:,}")
        self.date_var.set(profile.date)
        self.url_var.set(profile.profile_url)

        for item in self.tree.get_children():
            self.tree.delete(item)

        for article in profile.articles:
            year = str(article.year) if article.year else ""
            self.tree.insert(
                "",
                "end",
                values=(article.title, f"{article.citations:,}", year),
            )

        self.status_var.set(status)


def launch_gui(
    user_id: str = DEFAULT_USER_ID,
    max_age_days: float = DEFAULT_CACHE_DAYS,
    profile_file: Path = DEFAULT_PROFILE_FILE,
) -> None:
    global messagebox, tk, ttk
    import tkinter as tk
    from tkinter import messagebox, ttk

    root = tk.Tk()
    ScholarReaderApp(
        root, user_id=user_id, max_age_days=max_age_days, profile_file=profile_file
    )
    root.mainloop()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Cache-aware Google Scholar profile scraper and Tkinter viewer."
    )
    parser.add_argument(
        "--user-id", default=os.environ.get("SCHOLAR_USER_ID", DEFAULT_USER_ID)
    )
    parser.add_argument("--max-age-days", type=float, default=DEFAULT_CACHE_DAYS)
    parser.add_argument("--profile-file", type=Path, default=DEFAULT_PROFILE_FILE)
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="Ignore the cache age and request fresh data.",
    )
    parser.add_argument(
        "--no-gui",
        action="store_true",
        help="Print JSON to the terminal instead of opening Tkinter.",
    )
    args = parser.parse_args()

    max_age_days = -1 if args.force_refresh else args.max_age_days
    if args.no_gui:
        profile, status = get_profile(args.user_id, max_age_days, args.profile_file)
        payload = profile_to_dict(profile)
        payload["status"] = status
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return

    launch_gui(args.user_id, args.max_age_days, args.profile_file)


if __name__ == "__main__":
    main()
