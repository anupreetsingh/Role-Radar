"""Setup's pages, the Mac app's SetupView: the person's profession (which brings its company list and
job titles), their countries, the companies tracked, the roles they want and don't, their
qualifications, and alerts (optional). Leaving a page saves it; closing the window does too.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QFrame, QHBoxLayout, QLineEdit, QPlainTextEdit, QPushButton, QRadioButton, QScrollArea,
    QSpinBox, QVBoxLayout, QWidget,
)

from role_radar_app import place, system
from role_radar_app.cli import Result
from role_radar_app.model import Model
from role_radar_app.views import style
from role_radar_app.views.style import MEDIUM, SEMIBOLD, px, sized
from role_radar_app.views.widgets import (
    InfoButton, Spinner, Trouble, button, chips, clear, column, label, link, row, ticked_example,
)

PAGES = ["Profession", "Countries", "Companies", "Roles", "Qualifications", "Alerts"]
LAST = len(PAGES) - 1
DEGREES = [("none", "No degree yet"), ("bachelors", "Bachelor's"), ("masters", "Master's"), ("phd", "PhD")]
SYMBOLS = {"tech": "💻", "accounting": "📊", "healthcare": "🩺"}
SITE_NAMES = {"oracle_hcm": "Oracle", "smartrecruiters": "SmartRecruiters", "icims": "iCIMS", "bamboohr": "BambooHR",
              "hrmdirect": "HRM Direct", "tiktok": "TikTok"}
APP_PASSWORDS = "https://myaccount.google.com/apppasswords"
SAVED_WHERE = "Saved in Windows Credential Manager" if place.WINDOWS else "Saved in your Mac's Keychain"


def lines(text: str) -> list[str]:
    """One per line or comma, trimmed."""
    return [part.strip() for part in text.replace(",", "\n").splitlines() if part.strip()]


def boxes(saved: list[str], offered: list[str]) -> tuple[set[str], list[str]]:
    """The boxes for what's saved: the offered ones ticked (any capitalization), and their own after."""
    ticked: set[str] = set()
    own: list[str] = []
    for word in saved:
        known = next((o for o in offered if o.casefold() == word.casefold()), None)
        if known:
            ticked.add(known)
        else:
            own.append(word)
            ticked.add(word)
    return ticked, own


def still_alert(skip_from: int) -> str:
    """What someone skipping jobs that ask for `skip_from`+ years still hears about."""
    return {1: "no experience", 2: "no experience or 1+ year", 3: "no experience, 1+ or 2+ years"}.get(
        skip_from, f"anything from no experience up to {skip_from - 1}+ years")


def about_company(company: dict[str, Any]) -> str:
    """"Workday · US, CA", and why it isn't tracked unless they turned it off themselves."""
    parts = []
    site = company.get("site")
    if site and site != "generic":
        parts.append(SITE_NAMES.get(site, site.capitalize()))
    if company.get("countries"):
        parts.append(", ".join(company["countries"]))
    if company.get("why") and not company.get("off"):
        parts.append(f"not tracked: {company['why']}")
    return " · ".join(parts)


class ProfessionCard(QFrame):
    """A profession on the first page: it lights up under the pointer, and shows a tick (a spinner
    while saving) once picked."""

    clicked = Signal()

    def __init__(self, option: dict[str, Any]) -> None:
        super().__init__()
        self.option = option
        self.setObjectName("profession")
        style.hand(self)
        self.setAccessibleName(option["name"])
        self.mark = label("", 18, wrap=False, tone="accent")
        self.spinner = Spinner()
        self.spinner.hide()
        top = row(label(SYMBOLS.get(option["id"], "💼"), 26, wrap=False), self.mark, self.spinner, stretch_at=1)
        layout = column(8, margins=16)
        layout.addLayout(top)
        layout.addWidget(label(option["name"], 15, SEMIBOLD))
        layout.addWidget(label(option["about"], 12, tone="secondary"))
        layout.addStretch(1)
        self.setLayout(layout)
        self._hover = self.chosen = self.saving = False
        self._paint()

    def show_choice(self, chosen: bool, saving: bool, enabled: bool) -> None:
        self.chosen, self.saving = chosen, saving
        self.setEnabled(enabled or chosen)
        self._paint()

    def _paint(self) -> None:
        accent = style.accent()
        if self.chosen:
            fill, edge, width = style.faded(accent, 0.12), accent, 2
        elif self._hover and self.isEnabled():
            fill, edge, width = style.faded(style.text(), 0.08), style.faded(accent, 0.5), 1.5
        else:
            fill, edge, width = style.faded(style.text(), 0.04), style.faded(style.text(), 0.25), 1
        self.setStyleSheet(f"QFrame#profession {{ background: {style.css(fill)}; border: {width}px solid {style.css(edge)};"
                           " border-radius: 12px; }")
        self.mark.setText("✔" if self.chosen and not self.saving else "")
        self.spinner.setVisible(self.saving)

    def enterEvent(self, event) -> None:
        self._hover = True
        self._paint()

    def leaveEvent(self, event) -> None:
        self._hover = False
        self._paint()

    def mousePressEvent(self, event) -> None:
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()) and self.isEnabled():
            self.clicked.emit()


class TitleBoxes(QWidget):
    """A set of job title boxes in groups (target roles, or non-target words), with their own after,
    and a button that opens a field for adding one."""

    changed = Signal()

    def __init__(self, add_placeholder: str, add_button: str, all_buttons: bool) -> None:
        super().__init__()
        self.groups: list[dict[str, Any]] = []
        self.ticked: set[str] = set()
        self.own: list[str] = []
        self.layout_ = column(10)
        self.chips = QWidget()
        self.chips_layout = column(10)
        self.chips.setLayout(self.chips_layout)
        if all_buttons:
            self.count = label("", 11, tone="secondary", wrap=False)
            self.layout_.addLayout(row(button("Tick All", lambda: self._set_all(True), size=11),
                                       button("Untick All", lambda: self._set_all(False), size=11),
                                       self.count, stretch_at=-1))
        else:
            self.count = None
        self.layout_.addWidget(self.chips)
        # Adding their own: a field only once asked for.
        self.field = QLineEdit()
        self.field.setPlaceholderText(add_placeholder)
        self.field.setMaximumWidth(px(280))
        sized(self.field, 12)
        self.field.returnPressed.connect(self._add)
        self.add = button("Add", self._add, size=12)
        self.done = button("Done", self._close_adder, size=12)
        self.field.textChanged.connect(lambda text: self.add.setEnabled(bool(text.strip())))
        self.adder = QWidget()
        self.adder.setLayout(row(self.field, self.add, self.done, stretch_at=-1))
        self.adder.hide()
        self.opener = button(add_button, self._open_adder, size=11)
        self.layout_.addWidget(self.adder)
        self.layout_.addLayout(row(self.opener, stretch_at=-1))
        self.setLayout(self.layout_)

    @property
    def offered(self) -> list[str]:
        return [title for group in self.groups for title in group["titles"]]

    def fill(self, groups: list[dict[str, Any]], saved: list[str]) -> None:
        self.groups = groups
        self.ticked, self.own = boxes(saved, self.offered)
        self._draw()

    def chosen(self) -> list[str]:
        """The ticked boxes, the profession's in order, then their own."""
        return [t for t in self.offered if t in self.ticked] + [t for t in self.own if t in self.ticked]

    def _draw(self) -> None:
        clear(self.chips_layout)
        sections = [(g["name"], g["titles"]) for g in self.groups] + ([("Your own", self.own)] if self.own else [])
        for name, titles in sections:
            self.chips_layout.addWidget(label(name, 11, MEDIUM, tone="secondary"))
            self.chips_layout.addWidget(chips(titles, self.ticked, self._toggle))
        self._count()

    def _count(self) -> None:
        if self.count:
            self.count.setText(f"{len(self.ticked)} ticked")

    def _toggle(self, title: str, on: bool) -> None:
        (self.ticked.add if on else self.ticked.discard)(title)
        self._count()
        self.changed.emit()

    def _set_all(self, on: bool) -> None:
        self.ticked = set(self.offered + self.own) if on else set()
        self._draw()
        self.changed.emit()

    def _open_adder(self) -> None:
        self.adder.show()
        self.opener.hide()
        self.add.setEnabled(False)
        self.field.setFocus()

    def _close_adder(self) -> None:
        self.field.clear()
        self.adder.hide()
        self.opener.show()

    def _add(self) -> None:
        """Tick what was typed: the box already offered under any capitalization, or a new one of their own."""
        word = self.field.text().strip()
        if not word:
            return
        known = next((t for t in self.offered + self.own if t.casefold() == word.casefold()), None)
        if known:
            self.ticked.add(known)
        else:
            self.own.append(word)
            self.ticked.add(word)
        self.field.clear()
        self._draw()
        self.changed.emit()


class SetupView(QWidget):
    finished = Signal()  # Start Checking or Done: the window turns into Live Tracking

    def __init__(self, model: Model) -> None:
        super().__init__()
        self.model = model
        self.page = 0
        self.busy: str | None = None  # the action working right now
        self.choosing: str | None = None  # the profession being saved, shown as picked meanwhile
        self.filled = False
        self.saved = ""  # the profile pages' answers as last saved, to tell when they've changed
        self.found: dict[str, Any] | None = None  # the Companies page's search
        self.shown_results = 50
        self.turning: set[str] = set()
        self._notes: dict[str, tuple[str, bool] | None] = {}  # each action's last result
        self._search_timer = QTimer(self, singleShot=True, interval=300, timeout=self._search)

        header = column(12)
        header.setContentsMargins(24, 24, 24, 14)
        header.addWidget(label("Set Up Role Radar", 20, SEMIBOLD))
        self.markers = QHBoxLayout()
        self.markers.setSpacing(4)
        header.addLayout(self.markers)

        self.trouble = Trouble(lambda: self.model.load_setup(then=self.load))
        self.loading = label("Loading…", 13, tone="secondary")
        self.loading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # Only the page shown takes space, so a short page isn't spread over the tallest one's height.
        self.pages = [build() for build in (self._profession_page, self._countries_page, self._companies_page,
                                            self._roles_page, self._qualifications_page, self._alerts_page)]
        body = QWidget()
        body_layout = column(0, margins=24)
        body_layout.addWidget(self.trouble)
        body_layout.addWidget(self.loading)
        for page in self.pages:
            body_layout.addWidget(page)
        body_layout.addStretch(1)
        body.setLayout(body_layout)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setWidget(body)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)

        layout = column(0)
        layout.addLayout(header)
        layout.addWidget(_divider())
        layout.addWidget(self.scroll, 1)
        layout.addWidget(_divider())
        layout.addWidget(self._footer())
        self.setLayout(layout)
        self.setMinimumSize(px(640), px(600))

        model.setup_changed.connect(self._show)
        model.state_changed.connect(self._show_footer)
        self._show()

    # -- loading and showing ----------------------------------------------------------------------

    @property
    def setup(self) -> dict[str, Any] | None:
        return self.model.setup

    @property
    def profession(self) -> dict[str, Any] | None:
        setup = self.setup or {}
        return next((p for p in setup.get("professions") or [] if p["id"] == setup.get("profession")), None)

    def showEvent(self, event) -> None:
        self.model.load_setup(then=self.load)

    def load(self) -> None:
        """Fill the pages from what's saved, once, and open where there's something left to do."""
        setup = self.setup
        if self.filled or not setup:
            self._show()
            return
        self._fill_titles()
        self.countries = set(setup.get("countries") or [])
        self.cities.setText(", ".join(setup.get("cities") or []))
        # On until the countries page is saved: then it's what they chose.
        self.check_years.setChecked(setup.get("max_experience_years") is not None or not setup.get("countries"))
        self.skip_from.setValue((setup.get("max_experience_years") if setup.get("max_experience_years") is not None else 2) + 1)
        self.address.setText(setup.get("email") or "")
        self.also.setPlainText("\n".join(setup.get("also") or []))
        self._set_education(setup.get("education") or "bachelors")
        self._show_years()
        self.saved = self.fingerprint()
        self.page = (0 if not setup.get("profession") else 1 if not setup.get("countries") else 3 if not setup.get("roles")
                     else 4 if setup.get("education") is None else 0)
        self.filled = True
        self._show()

    def _fill_titles(self) -> None:
        setup, profession = self.setup or {}, self.profession or {}
        self.targets.fill(profession.get("groups") or [], setup.get("roles") or [])
        self.skips.fill(profession.get("skip_groups") or [], setup.get("exclude") or [])

    def _show(self) -> None:
        setup = self.setup
        self.trouble.show_message(self.model.setup_error)
        self.loading.setVisible(setup is None and not self.model.setup_error)
        for index, page in enumerate(self.pages):
            page.setVisible(setup is not None and index == self.page)
        if setup is None:
            self._show_markers()
            self._show_footer()
            return
        self._show_profession()
        self._show_countries()
        self._show_companies()
        self._show_roles()
        self._show_alerts()
        self._show_markers()
        self._show_footer()

    def _show_markers(self) -> None:
        clear(self.markers)
        setup = self.setup or {}
        saved = bool(setup.get("countries"))
        done = [bool(setup.get("profession")), saved, saved, saved and bool(setup.get("roles")),
                setup.get("education") is not None, self._alerts_ready()]
        for index, name in enumerate(PAGES):
            marker = QPushButton(("✔ " if done[index] else f"{index + 1}. ") + name)
            marker.setProperty("flat", True)
            marker.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # Back and Next move between pages from the keyboard
            sized(marker, 12, SEMIBOLD if index == self.page else style.QFont.Weight.Normal)
            color = style.GREEN if done[index] else style.text() if index == self.page else style.secondary()
            marker.setStyleSheet(f"color: {style.css(color)};")
            marker.setEnabled(self.busy is None and (index == 0 or bool(setup.get("profession"))))
            marker.clicked.connect(lambda *_, i=index: self.go(i))
            self.markers.addWidget(marker)
            if index < LAST:
                self.markers.addWidget(label("›", 11, wrap=False, tone="tertiary"))
        self.markers.addStretch(1)

    # -- page 1: profession ----------------------------------------------------------------------------

    def _profession_page(self) -> QWidget:
        page = QWidget()
        layout = column(16)
        layout.addWidget(label("What's your profession?", 15, SEMIBOLD))
        layout.addWidget(label("Role Radar watches the job boards of companies that hire in your profession, and emails you "
                               "new jobs that match within minutes of them being posted.", 12, tone="secondary"))
        self.cards_row = QHBoxLayout()
        self.cards_row.setSpacing(12)
        layout.addLayout(self.cards_row)
        self.profession_note = label("", 12, tone="secondary")
        layout.addWidget(self.profession_note)
        page.setLayout(layout)
        self.cards: dict[str, ProfessionCard] = {}
        return page

    def _show_profession(self) -> None:
        options = (self.setup or {}).get("professions") or []
        if [c for c in self.cards] != [o["id"] for o in options]:
            clear(self.cards_row)
            self.cards = {}
            for option in options:
                card = ProfessionCard(option)
                card.clicked.connect(lambda o=option: self.pick(o["id"]))
                self.cards_row.addWidget(card, 1)
                self.cards[option["id"]] = card
        chosen = self.choosing or (self.setup or {}).get("profession")
        for pid, card in self.cards.items():
            card.show_choice(chosen == pid, self.choosing == pid, self.busy is None and self.choosing is None)
        note = self._notes.get("profession")
        if note and not note[1]:
            self.profession_note.setText(f"⚠ {note[0]}")
            style.tone(self.profession_note, "red")
        elif self.profession and not self.choosing:
            count = sum(len(g["titles"]) for g in self.profession["groups"])
            self.profession_note.setText(f"✔ {self.profession['name']}: {count} job titles to choose from, and its "
                                         "companies. Next, pick your countries and titles.")
            style.tone(self.profession_note, "secondary")
        else:
            self.profession_note.setText("")

    def pick(self, pid: str) -> None:
        """Show the choice straight away, save it, and stay on the page: Next moves on."""
        if pid == (self.setup or {}).get("profession") or self.choosing:
            return
        self.choosing = pid
        self._show()

        def saved(problem: str | None) -> None:
            self.choosing = None
            self._notes["profession"] = (problem, False) if problem else None
            if problem is None:
                self._fill_titles()
                self.saved = self.fingerprint()  # a new profession brings its own boxes, saved already
            self._show()

        self.model.setup_step(["profession"], {"profession": pid}, then=saved)

    # -- page 2: countries -------------------------------------------------------------------------------

    def _countries_page(self) -> QWidget:
        page = QWidget()
        layout = column(20)
        self.countries: set[str] = set()
        self.country_boxes = QVBoxLayout()
        self.country_boxes.setSpacing(10)
        self.untagged_note = label("", 12, tone="secondary")
        self.tracked_note = label("", 13, MEDIUM)
        where = _section("Where do you want to work?", "Role Radar tracks the companies that post jobs in the countries you pick.")
        where.addLayout(self.country_boxes)
        where.addWidget(self.untagged_note)
        where.addWidget(self.tracked_note)
        layout.addLayout(where)
        self.cities = QLineEdit()
        self.cities.setPlaceholderText("Cities, separated by commas")
        self.cities.setMaximumWidth(px(460))
        sized(self.cities, 13)
        cities = _section("Cities (optional)", "Alerts only for jobs in these cities, e.g. Bengaluru, Hyderabad. With none, "
                          "jobs anywhere in your countries alert you. Jobs listed only as \"Remote\" always do.")
        cities.addWidget(self.cities)
        layout.addLayout(cities)
        page.setLayout(layout)
        self._country_widgets: dict[str, QCheckBox] = {}
        return page

    def _picked_key(self) -> str:
        return "+".join(c["code"] for c in (self.setup or {}).get("country_options") or [] if c["code"] in self.countries)

    def tracked_count(self) -> int | None:
        return None if not self.countries else ((self.setup or {}).get("companies_for") or {}).get(self._picked_key())

    def _country_names(self) -> str:
        names = [c["name"] for c in (self.setup or {}).get("country_options") or [] if c["code"] in self.countries]
        return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1] if names else ""

    def _show_countries(self) -> None:
        setup = self.setup or {}
        options = setup.get("country_options") or []
        untagged = setup.get("companies_untagged") or 0
        if list(self._country_widgets) != [c["code"] for c in options]:
            clear(self.country_boxes)
            self._country_widgets = {}
            for country in options:
                box = QCheckBox()
                sized(box, 14)
                box.toggled.connect(lambda on, code=country["code"]: self._toggle_country(code, on))
                self.country_boxes.addWidget(box)
                self._country_widgets[country["code"]] = box
        # Each country's count is what ticking it alone tracks: those posting there, and those naming no country.
        for country in options:
            box = self._country_widgets[country["code"]]
            count = (setup.get("companies_by_country") or {}).get(country["code"]) or 0
            box.setText(country["name"] + (f"     {count + untagged:,} companies" if count else ""))
            box.blockSignals(True)
            box.setChecked(country["code"] in self.countries)
            box.blockSignals(False)
        self.untagged_note.setVisible(untagged > 0)
        self.untagged_note.setText(f"Each count includes {untagged:,} companies whose job listings don't name a country.")
        tracked = self.tracked_count()
        self.tracked_note.setVisible(tracked is not None)
        if tracked is not None:
            self.tracked_note.setText(f"{tracked:,} companies to track in {self._country_names()}.")
            style.tone(self.tracked_note, "orange" if tracked == 0 else None)

    def _toggle_country(self, code: str, on: bool) -> None:
        (self.countries.add if on else self.countries.discard)(code)
        self._show_countries()
        self._show_footer()

    # -- page 3: the companies tracked --------------------------------------------------------------------

    def _companies_page(self) -> QWidget:
        page = QWidget()
        layout = column(18)
        self.companies_head = column(6)
        layout.addLayout(self.companies_head)
        self.by_country = column(6)
        layout.addLayout(self.by_country)
        # Find a company: untick one to stop tracking it. With nothing typed, the ones turned off.
        self.find_section = QWidget()
        find = _section("Find a company", "Untick a company to stop tracking it; tick it again to bring it back.")
        self.query = QLineEdit()
        self.query.setPlaceholderText("Search companies")
        self.query.setClearButtonEnabled(True)
        self.query.setMaximumWidth(px(320))
        sized(self.query, 13)
        self.query.textChanged.connect(self._query_changed)
        find.addWidget(self.query)
        self.found_note = label("", 11, tone="secondary")
        find.addWidget(self.found_note)
        self.results = column(6)
        find.addLayout(self.results)
        self.more = button("Show More", self._show_more, size=11)
        find.addLayout(row(self.more, stretch_at=-1))
        self.find_problem = label("", 11, tone="red")
        find.addWidget(self.find_problem)
        self.find_section.setLayout(find)
        layout.addWidget(self.find_section)
        # Add a company they want: tracked at once if Role Radar can read its job site, and either way
        # suggested for everyone's list, unless it's listed already.
        add = _section("Add a company you want", "Not on the list? Give its name, and its careers page if you know it. "
                       "If Role Radar can read its job site, it's tracked right away; if not, we'll work on it.")
        self.new_company = QLineEdit()
        self.new_company.setPlaceholderText("Company name")
        self.new_company.setMaximumWidth(px(320))
        self.new_careers = QLineEdit()
        self.new_careers.setPlaceholderText("Careers page (optional), https://…")
        self.new_careers.setMaximumWidth(px(420))
        for field in (self.new_company, self.new_careers):
            sized(field, 13)
            add.addWidget(field)
        self.add_row = self._action("add", "Add", self._add_company)
        self.new_company.textChanged.connect(self._show_add)
        add.addWidget(self.add_row)
        layout.addLayout(add)
        page.setLayout(layout)
        return page

    def _show_companies(self) -> None:
        setup = self.setup or {}
        count = self.tracked_count()
        count = setup.get("companies") or 0 if count is None else count
        name = (self.profession or {}).get("name") or ""
        clear(self.companies_head)
        clear(self.by_country)
        if count == 0:
            head = _section(f"No {name} companies yet", f"Role Radar doesn't have a list of {name} employers yet. A later "
                            "version adds them, and checking starts then.")
            self.companies_head.addLayout(head)
        else:
            self.companies_head.addWidget(label(f"{count:,} companies will be tracked", 22, SEMIBOLD))
            self.companies_head.addWidget(label(
                "Role Radar checks every one of their job boards from this PC while it's on: most every 20 minutes, "
                "Workday boards every 6 hours. You'll hear about new jobs that match your roles within minutes of them "
                "being posted.", 13, tone="secondary"))
            by_country = _section("By country", None)
            for country in setup.get("country_options") or []:
                if country["code"] in self.countries:
                    posting = (setup.get("companies_by_country") or {}).get(country["code"]) or 0
                    by_country.addLayout(row(label(country["name"], 13, wrap=False),
                                             label(f"{posting:,} companies post jobs here", 13, tone="secondary", wrap=False),
                                             spacing=16, stretch_at=-1))
            if setup.get("companies_untagged"):
                by_country.addWidget(label(f"Also {setup['companies_untagged']:,} whose job listings don't name a country, "
                                           "so they're tracked for every country.", 12, tone="secondary"))
            self.by_country.addLayout(by_country)
        self.find_section.setVisible(count > 0)
        self._show_add()

    def _show_add(self) -> None:
        self.add_row.button.setEnabled(bool(self.new_company.text().strip()) and self.busy is None)

    def _query_changed(self) -> None:
        self.shown_results = 50
        self._search_timer.start()  # once they pause typing

    def _show_more(self) -> None:
        self.shown_results += 50
        self._search()

    def _search(self) -> None:
        """The companies matching what's typed (nothing typed: the ones turned off)."""
        typed = self.query.text()

        def found(result: Result) -> None:
            if typed != self.query.text():
                return  # they've typed more since
            if result.ok:
                try:
                    self.found, problem = result.json(), ""
                except ValueError:
                    self.found, problem = None, "Unexpected reply from role-radar"
            else:
                problem = result.message
            self.find_problem.setText(problem)
            self.find_problem.setVisible(bool(problem))
            self._show_results()

        self.model.setup_command(["find"], then=found, stdin=json.dumps(
            {"query": typed, "limit": self.shown_results, "which": "all" if typed else "off"}))

    def _show_results(self) -> None:
        clear(self.results)
        found = self.found
        if not found:
            self.found_note.hide()
            self.more.hide()
            return
        typed = self.query.text()
        if not typed:
            self.found_note.setText(f"Turned off ({found['total']})" if found["results"] else "")
        elif found["total"] == 0:
            self.found_note.setText("No company on the list matches. Add it below, or ask for it.")
        else:
            self.found_note.setText(f"{len(found['results'])} of {found['total']}" if found["total"] > len(found["results"])
                                    else f"{found['total']} found")
        self.found_note.setVisible(bool(self.found_note.text()))
        for company in found["results"]:
            box = QCheckBox()
            box.setChecked(not company["off"])
            box.setEnabled(company["readable"] and company["name"] not in self.turning)
            box.setAccessibleName(company["name"])
            box.toggled.connect(lambda on, c=company: self._set_tracked(c, on))
            name = label(company["name"], 13, wrap=False)
            mine = label("Yours", 10, MEDIUM, tone="secondary", wrap=False) if company.get("own") else None
            text = column(1)
            text.addLayout(row(name, mine, spacing=6, stretch_at=-1))
            text.addWidget(label(about_company(company), 11, tone="secondary"))
            line = QHBoxLayout()
            line.setSpacing(8)
            line.addWidget(box, 0, Qt.AlignmentFlag.AlignTop)
            line.addLayout(text, 1)
            self.results.addLayout(line)
        self.more.setVisible(found["total"] > len(found["results"]))

    def _set_tracked(self, company: dict[str, Any], on: bool) -> None:
        self.turning.add(company["name"])

        def done(problem: str | None) -> None:
            self.turning.discard(company["name"])
            self.find_problem.setText(problem or "")
            self.find_problem.setVisible(bool(problem))
            self._search()

        self.model.setup_step(["track"], {"names": [company["name"]], "tracked": on}, then=done)

    def _add_company(self, finish: Callable[[tuple[str, bool]], None]) -> None:
        gave_careers = bool(self.new_careers.text().strip())

        def added(result: Result) -> None:
            if not result.ok:
                finish((result.message, False))
                return
            try:
                outcome = result.json()
            except ValueError:
                finish(("Unexpected reply from role-radar", False))
                return
            name = outcome.get("name")
            if outcome.get("status") == "added":
                jobs = outcome.get("jobs")
                listed = "" if jobs is None else " (1 job listed now)" if jobs == 1 else f" ({jobs} jobs listed now)"
                note = (f"Added: Role Radar now tracks {name}{listed}.", True)
                self.new_company.clear()
                self.new_careers.clear()
            elif outcome.get("status") == "listed":
                if outcome.get("why"):
                    note = (f"{name} is on the list, but isn't tracked: {outcome['why']}.", False)
                else:
                    note = (f"{name} was turned off; it's tracked again." if outcome.get("turned_on")
                            else f"{name} is tracked already.", True)
            else:
                hint = " If you know its careers page, add it and try again." if not gave_careers and not outcome.get("url") else ""
                note = (f"Role Radar can't track {name} yet. We've noted it and are working on it.{hint}", False)
            finish(note)
            self.model.load_setup()
            self._search()

        self.model.setup_command(["add"], then=added,
                                 stdin=json.dumps({"name": self.new_company.text(), "url": self.new_careers.text()}))

    # -- page 4: roles ---------------------------------------------------------------------------------

    def _roles_page(self) -> QWidget:
        page = QWidget()
        layout = column(28)
        self.targets = TitleBoxes("A title of your own", "Add a Title…", all_buttons=True)
        self.skips = TitleBoxes("A word of your own", "Add a Word…", all_buttons=False)
        self.targets.changed.connect(self._show_footer)
        self.target_head = QHBoxLayout()
        self.skip_head = QHBoxLayout()
        targets = column(10)
        targets.addLayout(self.target_head)
        targets.addWidget(label("A job alerts you when its title contains one of the ticked titles. Untick any you don't want.",
                                12, tone="secondary"))
        targets.addWidget(self.targets)
        skips = column(10)
        skips.addLayout(self.skip_head)
        skips.addWidget(label("A ticked word here stops a job's alert, unless the word is part of a target title you ticked. "
                              "Untick any you want, such as Senior or Lead if you have the experience.", 12, tone="secondary"))
        skips.addWidget(self.skips)
        layout.addLayout(targets)
        layout.addLayout(skips)
        page.setLayout(layout)
        self._examples_for: str | None = None
        return page

    def _show_roles(self) -> None:
        profession = self.profession or {}
        if self._examples_for == profession.get("id"):
            return
        self._examples_for = profession.get("id")
        examples = profession.get("examples") or {}
        for head, title, info in ((self.target_head, "Target roles", self._target_info(examples.get("target"))),
                                  (self.skip_head, "Non-target roles", self._non_target_info(examples.get("non_target")))):
            clear(head)
            head.setSpacing(6)
            head.addWidget(label(title, 15, SEMIBOLD, wrap=False))
            if info:
                head.addWidget(info)
            head.addStretch(1)

    @staticmethod
    def _target_info(example: dict[str, Any] | None) -> InfoButton | None:
        """The ⓘ beside target roles: what a ticked title does, with an example."""
        if not example:
            return None
        rule = "You get an alert when a ticked title is in a job's title."
        return InfoButton(rule, lambda: ticked_example(rule, [example["title"]], [], example["jobs"], []))

    @staticmethod
    def _non_target_info(example: dict[str, Any] | None) -> InfoButton | None:
        """The ⓘ beside non-target roles: what a ticked word does, and that it never stops a target title it's part of."""
        if not example:
            return None
        rule = "A ticked word here stops the alert, unless it's part of a target title you ticked."
        return InfoButton(rule, lambda: ticked_example(rule, example["targets"], example["words"], example["reach"],
                                                      example["stopped"]))

    # -- page 5: qualifications -----------------------------------------------------------------------

    def _qualifications_page(self) -> QWidget:
        page = QWidget()
        layout = column(24)
        experience = _section("Experience", "Role Radar reads each new match's description once, and skips jobs asking for "
                              "more experience than you have.")
        self.check_years = QCheckBox("Skip jobs asking for")
        sized(self.check_years, 13)
        self.skip_from = QSpinBox()
        self.skip_from.setRange(1, 20)
        self.skip_from.setSuffix("+ years of experience")
        sized(self.skip_from, 13)
        self.years_note = label("", 12, tone="secondary")
        self.check_years.toggled.connect(self._show_years)
        self.skip_from.valueChanged.connect(self._show_years)
        experience.addLayout(row(self.check_years, self.skip_from, spacing=10, stretch_at=-1))
        experience.addWidget(self.years_note)
        layout.addLayout(experience)
        education = _section("Education", "Your highest degree, or the one you'll have when you start. Jobs that need a "
                             "higher degree are skipped.")
        self.degrees = QButtonGroup(self)
        for value, name in DEGREES:
            choice = QRadioButton(name)
            choice.setProperty("degree", value)
            sized(choice, 13)
            self.degrees.addButton(choice)
            education.addWidget(choice)
        education.addWidget(label("A degree can also count in place of experience: if you skip 3+ years and have a Master's, "
                                  "a job asking for \"3 years, or 1 year with a Master's\" is still shown.", 12, tone="secondary"))
        layout.addLayout(education)
        page.setLayout(layout)
        self._set_education("bachelors")
        self._show_years()
        return page

    def _show_years(self) -> None:
        self.skip_from.setEnabled(self.check_years.isChecked())
        self.years_note.setText(f"You'll still hear about jobs asking for {still_alert(self.skip_from.value())}, and jobs "
                                "that don't say." if self.check_years.isChecked()
                                else "Jobs alert you whatever experience they ask for.")

    def _set_education(self, value: str) -> None:
        for choice in self.degrees.buttons():
            choice.setChecked(choice.property("degree") == value)

    def education(self) -> str:
        checked = self.degrees.checkedButton()
        return checked.property("degree") if checked else "bachelors"

    # -- page 6: alerts --------------------------------------------------------------------------------

    def _alerts_page(self) -> QWidget:
        page = QWidget()
        layout = column(28)
        layout.addWidget(label("Alerts are optional. New jobs always collect in Live Tracking, newest on top, so you can just "
                               "open the app to see them. Set up email, Discord or both to also get them sent every 10 "
                               "minutes.", 13))
        email = _section("Email (Gmail)", "Alerts come from your own Gmail, sent to yourself and anyone you add. Gmail needs "
                         "an app password for this: a 16-letter password just for Role Radar. Creating one needs 2-Step "
                         "Verification on your Google account.")
        email.addLayout(row(link("Create an app password ↗", lambda: system.open_url(APP_PASSWORDS)), stretch_at=-1))
        self.address = QLineEdit()
        self.address.setPlaceholderText("you@gmail.com")
        self.password = QLineEdit()
        self.password.setPlaceholderText("App password (16 letters)")
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        for field in (self.address, self.password):
            field.setMaximumWidth(px(320))
            sized(field, 13)
            field.textChanged.connect(lambda: self._show_alerts())
            email.addWidget(field)
        self.email_row = self._action("email", "Save", self._save_email)
        email.addWidget(self.email_row)
        self.email_saved = QWidget()
        saved = column(8)
        saved.addWidget(label("Also send alerts to (a friend, your school email), one per line. Each person sees only your "
                              "address.", 12))
        self.also = QPlainTextEdit()
        self.also.setPlaceholderText("friend@example.com")
        self.also.setFixedHeight(px(64))
        self.also.setMaximumWidth(px(420))
        sized(self.also, 12, mono=True)
        saved.addWidget(self.also)
        self.also_row = self._action("also", "Save List", self._save_also)
        self.test_email = self._action("test-email", "Send Test Email", lambda finish: self._send_test("email", finish))
        saved.addLayout(row(self.also_row, self.test_email, spacing=12, stretch_at=-1))
        self.recipients = label("", 11, tone="secondary")
        saved.addWidget(self.recipients)
        self.email_saved.setLayout(saved)
        email.addWidget(self.email_saved)
        layout.addLayout(email)

        discord = _section("Discord", "Alerts go to a channel in your Discord server. In Discord, open the channel's "
                           "settings, then Integrations → Webhooks → New Webhook → Copy Webhook URL, and paste it here.")
        self.webhook = QLineEdit()
        self.webhook.setEchoMode(QLineEdit.EchoMode.Password)
        self.webhook.setMaximumWidth(px(420))
        sized(self.webhook, 13)
        self.webhook.textChanged.connect(lambda: self._show_alerts())
        discord.addWidget(self.webhook)
        self.discord_row = self._action("discord", "Save", self._save_discord)
        self.test_discord = self._action("test-discord", "Send Test Message", lambda finish: self._send_test("discord", finish))
        discord.addLayout(row(self.discord_row, self.test_discord, spacing=12, stretch_at=-1))
        layout.addLayout(discord)
        page.setLayout(layout)
        return page

    def _alerts_ready(self) -> bool:
        """At least one way to send alerts is set up."""
        setup = self.setup or {}
        return bool(setup.get("email_ready") or setup.get("discord_ready"))

    def _show_alerts(self) -> None:
        setup = self.setup or {}
        idle = self.busy is None
        self.email_row.button.setEnabled(idle and bool(self.address.text()) and bool(self.password.text()))
        ready = bool(setup.get("email") and setup.get("email_ready"))
        self.email_saved.setVisible(ready)
        if ready:
            others = len(setup.get("also") or [])
            self.recipients.setText(f"Alerts go to {setup['email']}"
                                    + ("." if not others else f" and {others} other{'' if others == 1 else 's'}."))
        discord_ready = bool(setup.get("discord_ready"))
        self.webhook.setPlaceholderText("Saved. Paste a new one to change it." if discord_ready
                                        else "https://discord.com/api/webhooks/...")
        self.discord_row.button.setEnabled(idle and bool(self.webhook.text().strip()))
        self.test_discord.setVisible(discord_ready)
        for action in (self.also_row, self.test_email, self.test_discord):
            action.button.setEnabled(idle)

    def _save_email(self, finish: Callable[[tuple[str, bool]], None]) -> None:
        def saved(problem: str | None) -> None:
            finish((problem, False) if problem else (SAVED_WHERE, True))
            if problem is None:
                self.password.clear()
                self.model.set("email", True)  # set up, so alerts go there

        self.model.setup_step(["email"], {"address": self.address.text(), "password": self.password.text()}, then=saved)

    def _save_discord(self, finish: Callable[[tuple[str, bool]], None]) -> None:
        def saved(problem: str | None) -> None:
            finish((problem, False) if problem else (SAVED_WHERE, True))
            if problem is None:
                self.webhook.clear()
                self.model.set("discord", True)

        self.model.setup_step(["discord"], {"webhook": self.webhook.text()}, then=saved)

    def _save_also(self, finish: Callable[[tuple[str, bool]], None]) -> None:
        self.model.setup_step(["recipients"], {"also": lines(self.also.toPlainText())},
                              then=lambda problem: finish((problem, False) if problem else ("Saved", True)))

    def _send_test(self, channel: str, finish: Callable[[tuple[str, bool]], None]) -> None:
        def sent(result: Result) -> None:
            finish((result.message, False) if not result.ok else
                   ("Sent. Check your inbox." if channel == "email" else "Sent. Check the channel.", True))

        self.model.calls.call(["notifications", "test", "--channel", channel], then=sent)

    def _action(self, key: str, title: str, action: Callable[[Callable[[tuple[str, bool]], None]], None]) -> ActionRow:
        """A button whose action keeps the page busy (every other button waits) until it's done."""
        def busy(on: bool) -> None:
            self.busy = key if on else None
            self._show()

        return ActionRow(title, action, busy)

    # -- the footer, and moving between pages ------------------------------------------------------------

    def _footer(self) -> QWidget:
        footer = QWidget()
        self.back = button("Back", lambda: self.go(self.page - 1), size=13)
        self.footer_note = label("", 12, tone="secondary")
        self.footer_spinner = Spinner()
        self.footer_spinner.hide()
        self.next = button("Next", self.next_page, size=13, primary=True)
        self.next.setDefault(True)
        self.start = button("Start Checking", self._start, size=13, primary=True)
        layout = QHBoxLayout()
        layout.setContentsMargins(24, 14, 24, 14)
        layout.setSpacing(10)
        layout.addWidget(self.back)
        layout.addWidget(self.footer_note, 1)
        layout.addWidget(self.footer_spinner)
        layout.addWidget(self.next)
        layout.addWidget(self.start)
        footer.setLayout(layout)
        return footer

    def _show_footer(self) -> None:
        if not hasattr(self, "start"):
            return
        setup = self.setup or {}
        self.back.setVisible(self.page > 0)
        self.back.setEnabled(self.busy is None)
        note = self._notes.get("page")
        if note and not note[1]:
            self.footer_note.setText(f"⚠ {note[0]}")
            style.tone(self.footer_note, "red")
        elif self.page == LAST:
            self.footer_note.setText("All set. Role Radar checks while this PC is on and the app is open, and it opens at login."
                                     if setup.get("ready") else "Pick your countries and roles to finish.")
            style.tone(self.footer_note, "secondary")
        else:
            self.footer_note.setText("")
        self.footer_spinner.setVisible(self.busy == "page")
        self.next.setVisible(self.page < LAST)
        self.next.setEnabled(self.busy is None and bool(setup.get("profession")) and not (self.page == 1 and not self.countries)
                             and not (self.page == 3 and not self.targets.ticked))
        self.start.setVisible(self.page == LAST)
        self.start.setText("Done" if self.model.checking == "laptop" else "Start Checking")
        self.start.setEnabled(bool(setup.get("ready")))

    def next_page(self) -> None:
        self.go(self.page + 1, always=True)

    def go(self, target: int, always: bool = False) -> None:
        """Move to another page. Leaving the countries, roles or qualifications page saves it first: always
        with Next, and with Back or a page's name whenever something changed, so no change is lost."""
        if target < 0 or target > LAST or self.busy:
            return
        self._notes["page"] = None
        if self.filled and (self.fingerprint() != self.saved or (always and self.page in (1, 3, 4))):
            self.busy = "page"
            self._show_footer()

            def saved(problem: str | None) -> None:
                self.busy = None
                if problem:
                    self._notes["page"] = (problem, False)
                    self._show_footer()
                    return
                self.saved = self.fingerprint()
                self._move(target)

            self.model.setup_step(["profile"], self.profile_data(), then=saved)
            return
        self._move(target)

    def _move(self, target: int) -> None:
        self.page = target
        self._show()
        self.scroll.verticalScrollBar().setValue(0)  # each page opens at its top
        if target == 2:
            self._search()

    def _start(self) -> None:
        def started() -> None:
            self.finished.emit()  # the window turns into Live Tracking

        self.model.start_checking(then=started)

    def save_if_changed(self) -> None:
        """Closing the window, or going to Live Tracking, keeps what was changed, as leaving the page does."""
        if self.filled and self.fingerprint() != self.saved:
            data = self.profile_data()
            self.saved = self.fingerprint()
            self.model.setup_step(["profile"], data)

    def profile_data(self) -> dict[str, Any]:
        """The countries, roles and qualifications pages' answers, for `setup profile`. Education goes once
        its page is open: until then the saved one (or none) stands."""
        data: dict[str, Any] = {"roles": self.targets.chosen(), "exclude": self.skips.chosen(),
                                "locations": lines(self.cities.text()), "countries": sorted(self.countries),
                                "max_experience_years": self.skip_from.value() - 1 if self.check_years.isChecked() else None}
        if self.page == 4:
            data["education"] = self.education()
        return data

    def fingerprint(self) -> str:
        """The answers as they stand, whatever the page, to compare with `saved`."""
        data = self.profile_data()
        data["education"] = self.education()
        return json.dumps(data, sort_keys=True)

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.KeyPress and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) \
                and self.page < LAST and self.next.isEnabled() and not isinstance(self.focusWidget(), (QLineEdit, QPlainTextEdit)):
            self.next_page()
            return True
        return super().event(event)


class ActionRow(QWidget):
    """A button, and once it has run, how that went: the Mac app's actionRow. Its action gets a `finish`
    to call with (note, ok); `busy` hears when it starts and finishes."""

    def __init__(self, title: str, action: Callable[[Callable[[tuple[str, bool]], None]], None],
                 busy: Callable[[bool], None]) -> None:
        super().__init__()
        self.button = button(title, self._run, size=12)
        self.spinner = Spinner()
        self.spinner.hide()
        self.note = label("", 11)
        self.note.hide()
        self.action, self.busy = action, busy
        self.setLayout(row(self.button, self.spinner, self.note, stretch_at=-1))

    def _run(self) -> None:
        self.spinner.show()
        self.note.hide()
        self.busy(True)
        self.action(self._finish)

    def _finish(self, note: tuple[str, bool]) -> None:
        self.spinner.hide()
        self.busy(False)
        text, ok = note
        self.note.setText(("✓ " if ok else "⚠ ") + text)
        style.tone(self.note, "green" if ok else "red")
        self.note.show()


def _section(title: str, about: str | None) -> QVBoxLayout:
    layout = column(10)
    layout.addWidget(label(title, 15, SEMIBOLD))
    if about:
        layout.addWidget(label(about, 12, tone="secondary"))
    return layout


def _divider() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFrameShadow(QFrame.Shadow.Plain)
    line.setStyleSheet(f"color: {style.css(style.faded(style.text(), 0.12))};")
    return line
