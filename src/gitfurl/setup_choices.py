"""Setup's own questions, asked by gitfurl instead of Setup's wizard.

An Inno Setup installer asks in its wizard for the language, the setup type, the components,
the tasks (a desktop icon, extra options), the install folder and, on its last page, which
programs to start. gitfurl skips that wizard (on the native route Setup never runs; with
--unattended it runs silently), so it reads those questions from the setup data
(gitfurl.inno.choices, read-only), shows them, lets the user answer, and uses the answers:

  switches  Wine/Proton route with --unattended: Inno's own /LANG=, /TYPE=, /COMPONENTS=
            (only with a custom setup type, as Inno ignores it otherwise) and /TASKS= (with
            !name for a task left unchecked) go into the command line.
  entries   native route with --apply-setup-entries: the [Registry] and [Icons] entries whose
            conditions name components, tasks or languages are kept or skipped by the answers
            (the same evaluator as before, gitfurl.inno.Choice.decide).
  files     extract-inno: the same for the [Files] entries Inno Setup stores itself.
  wizard    Wine/Proton route without --unattended: Setup shows its own wizard and asks
            itself; gitfurl shows the questions for information only.
  native    native route without --apply-setup-entries: the answers would change nothing
            (gitfurl unpacks every FreeArc archive; Setup's own entries are not applied), so
            the questions are shown for information only.

Not seen, and said so: custom wizard pages and questions built by the setup's [Code]; which
files of a FreeArc archive belong to which component (the script's [Code] picks the archives,
so gitfurl unpacks every archive); [Run] postinstall entries are listed but never run by
gitfurl on the native route. Nothing here runs or writes anything; asking reads the terminal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from gitfurl.detect import clean

MODE_SWITCHES, MODE_ENTRIES, MODE_FILES = "switches", "entries", "files"
MODE_WIZARD, MODE_NATIVE = "wizard", "native"
APPLIED = (MODE_SWITCHES, MODE_ENTRIES, MODE_FILES)

FREEARC_NOTE = ("gitfurl unpacks every archive; it cannot tell which files belong to which "
                "component")
POSTINSTALL_NATIVE = "would be offered by Setup; not run by gitfurl"
HEADING = "Setup's choices"

# The setup's sections the answers act on, per mode ([Dirs] and [INI] are never written).
SECTIONS = {MODE_ENTRIES: ("[Icons]", "[Registry]"), MODE_FILES: ("[Files]",)}

MODE_TEXT = {
    MODE_SWITCHES: "the answers are passed to Setup with Inno's own switches",
    MODE_ENTRIES: ("the answers decide which of the setup's registry entries and shortcuts "
                   "that name components, tasks or languages are added"),
    MODE_FILES: ("the answers decide which of Inno Setup's own files that name components, "
                 "tasks or languages are extracted"),
    MODE_WIZARD: ("for information only: Setup shows its own wizard and asks these itself "
                  "(turn on the unattended install to answer them here)"),
    MODE_NATIVE: ("for information only: on this route the answers change nothing (gitfurl "
                  "unpacks every archive and does not apply Setup's own entries)"),
}


class ChoicesStopped(Exception):
    """The terminal closed (end of input) before every question was answered."""


@dataclass
class Picked:
    """The questions, the answers and how they are used."""

    choices: object  # gitfurl.inno.Choices
    choice: object  # gitfurl.inno.Choice
    mode: str
    how: str  # "asked", "the script's defaults (...)", "given on the command line", "shown"
    setup: object = None  # gitfurl.inno.Setup
    notes: list[str] = field(default_factory=list)

    @property
    def applied(self) -> bool:
        return self.mode in APPLIED

    def record(self) -> str:
        """'choices: components a, b; tasks x (not y); language English'."""
        return self.choices.summary(self.choice)

    def switches(self) -> list[str]:
        return self.choices.switches(self.choice)[0] if self.mode == MODE_SWITCHES else []

    def entry_choice(self):
        """The Choice for setup_entries / plan_inno_extract, or None when not applied."""
        return self.choice if self.mode in (MODE_ENTRIES, MODE_FILES) else None

    def lines(self) -> list[str]:
        """The plan's (and the approval's) lines."""
        from gitfurl.inno import choice_effects

        out = [f"{HEADING} ({self.how}): {MODE_TEXT[self.mode]}"]
        out.append(f"  {self.record()}" if self.applied else
                   f"  the script's defaults: {self.record()[len('choices: '):]}")
        if self.mode == MODE_SWITCHES:
            args, notes = self.choices.switches(self.choice)
            out.append("  switches: " + (" ".join(args) if args else "none needed"))
            out.extend(f"  note: {n}" for n in notes)
        if self.mode in (MODE_ENTRIES, MODE_FILES) and self.setup is not None:
            eff = [f"{sec} {n} ({kept} kept, {n - kept} left out)"
                   for sec, n, kept in choice_effects(self.setup, self.choice)
                   if sec in SECTIONS[self.mode]]
            out.append("  entries that name components, tasks or languages: "
                       + (", ".join(eff) if eff else "none, so the answers change nothing"))
        for p in self.choices.postinstall:
            what = (POSTINSTALL_NATIVE if self.mode in (MODE_ENTRIES, MODE_FILES, MODE_NATIVE)
                    else ("runs in a silent install" if p.silent else
                          "does not run in a silent install") if self.mode == MODE_SWITCHES
                    else "Setup offers it at the end")
            out.append(f"  postinstall: {clean(p.description, 80)}: {what}")
        if self.choices.default_dir:
            out.append(f"  install folder: Setup's default is {clean(self.choices.default_dir, 120)}"
                       + ("; gitfurl uses C:\\Games\\PREFIX" if self.mode != MODE_WIZARD else ""))
        out.extend(f"  note: {n}" for n in self.notes)
        out.extend(f"  not seen: {u}" for u in self.choices.unseen)
        return out


# --- reading ---------------------------------------------------------------------------------

def load(installer: Path):
    """(Setup, Choices), or None when the installer's setup data cannot be read (not Inno
    Setup, or refused). Reads the installer's headers only."""
    from gitfurl import inno

    try:
        setup = inno.read_setup(Path(installer))
    except Exception:  # noqa: BLE001 - a hostile file must not stop the plan
        return None
    return setup, inno.choices(setup)


def names(text: str | None) -> list[str] | None:
    """'a, b,!c' -> ['a', 'b', '!c']; None stays None."""
    if text is None:
        return None
    return [n.strip() for n in text.split(",") if n.strip()]


def given(language=None, setup_type=None, components=None, tasks=None) -> dict:
    """The answers given as options (only those that were given)."""
    raw = {"language": language, "setup_type": setup_type, "components": names(components),
           "tasks": names(tasks)}
    return {k: v for k, v in raw.items() if v is not None}


# --- asking on a terminal --------------------------------------------------------------------

def _read(prompt: str, input_fn) -> str:
    try:
        return (input_fn or input)(prompt).strip()
    except EOFError as exc:
        raise ChoicesStopped("no answer (end of input)") from exc


def yes_no(prompt: str, default: bool, input_fn=None, out=print) -> bool:
    """A y/n question with the default in capitals; asks again until the answer is valid."""
    while True:
        got = _read(f"{prompt} {'[Y/n]' if default else '[y/N]'} ", input_fn).casefold()
        if not got:
            return default
        if got in ("y", "yes"):
            return True
        if got in ("n", "no"):
            return False
        out("  Please answer y or n (Enter keeps the default).")


def number(title: str, options: list[str], default: int, input_fn=None, out=print) -> int:
    """A numbered choice (0-based result); asks again until the answer is valid."""
    out(f"  {title}:")
    for k, text in enumerate(options, 1):
        out(f"    {k}) {text}" + ("  (default)" if k - 1 == default else ""))
    while True:
        got = _read(f"  Choose 1-{len(options)} [{default + 1}]: ", input_fn)
        if not got:
            return default
        if got.isdigit() and 1 <= int(got) <= len(options):
            return int(got) - 1
        out(f"  Please enter a number from 1 to {len(options)} (Enter keeps the default).")


def _label(item) -> str:
    return f"{clean(item.description or item.name, 80)} ({clean(item.name, 40)})"


def _ask_items(what: str, items, on: set[str], input_fn, out) -> dict[str, bool]:
    """Ask for each item in order: a y/n question, or a numbered choice for an exclusive
    group. Children of an item answered "no" are not asked. Returns the answers."""
    answers: dict[str, bool] = {}
    state = set(on)
    done_groups: set[str] = set()
    keys = {i.key for i in items}
    group = ""
    for i in items:
        if i.parent and i.parent in keys and i.parent not in state:
            continue  # its parent is off
        if getattr(i, "group", "") and i.group != group:
            group = i.group
            out(f"  {clean(group, 80)}")
        if i.fixed:
            out(f"  {what.title()} {_label(i)}: fixed, "
                + ("always installed" if i.key in state else "not installed")
                + " with this setup type")
            continue
        if i.exclusive:
            if i.parent in done_groups:
                continue
            done_groups.add(i.parent)
            members = [m for m in items if m.exclusive and m.parent == i.parent and not m.fixed]
            current = next((k for k, m in enumerate(members) if m.key in state), None)
            options = [_label(m) for m in members]
            if current is None:
                options.append("none of these")
            pick = number(f"{what.title()}s, choose one", options,
                          len(members) if current is None else current, input_fn, out)
            for k, m in enumerate(members):
                answers[m.key] = k == pick
                (state.add if k == pick else state.discard)(m.key)
            continue
        value = yes_no(f"  {what.title()} {_label(i)}?", i.key in state, input_fn, out)
        answers[i.key] = value
        (state.add if value else state.discard)(i.key)
    return answers


def ask(ch, input_fn=None, out=print):
    """Ask each of Setup's questions on the terminal, in the wizard's order. Returns the
    answered gitfurl.inno.Choice. Raises ChoicesStopped at the end of input."""
    base = ch.defaults()
    lang, stype = base.language, base.setup_type
    out(f"{HEADING} (Enter keeps the script's default):")
    if len(ch.languages) > 1:
        keys = [x.name.casefold() for x in ch.languages]
        k = number("Language", [f"{clean(x.display or x.name, 40)} ({clean(x.name, 40)})"
                                for x in ch.languages],
                   keys.index(lang) if lang in keys else 0, input_fn, out)
        lang = keys[k]
    if len(ch.types) > 1:
        keys = [t.name.casefold() for t in ch.types]
        k = number("Setup type", [f"{clean(t.description or t.name, 60)} ({clean(t.name, 40)})"
                                  + (" - you pick the components" if t.custom else "")
                                  for t in ch.types],
                   keys.index(stype) if stype in keys else 0, input_fn, out)
        stype = keys[k]
    comps: dict[str, bool] = {}
    if ch.components:
        shown = ch.shown_components(lang)
        on = ch.for_type(stype, lang)
        if ch.components_free(stype):
            comps = _ask_items("component", shown, on, input_fn, out)
        else:
            t = ch.type_of(stype)
            out(f"  Components: set by the setup type {clean(t.name if t else stype, 40)}: "
                + (", ".join(clean(i.name, 40) for i in shown if i.key in on) or "none"))
    on = ch.resolve(lang, stype, comps).components
    tasks: dict[str, bool] = {}
    if ch.tasks:
        shown = ch.shown_tasks(on, lang)
        if shown:
            from gitfurl.inno import _consistent

            tasks = _ask_items("task", shown, _consistent(shown, {t.key for t in shown
                                                                   if t.default}),
                               input_fn, out)
    return ch.resolve(lang, stype, comps, tasks)


def pick(installer: Path, mode: str, *, answers: dict | None = None, yes: bool = False,
         tty: bool = False, dry_run: bool = False, input_fn=None, out=print):
    """Read Setup's questions and get the answers for `mode`: asked on the terminal, taken from
    `answers` (the options), or the script's defaults (--yes, no terminal, a dry run, or a
    mode where the answers are for information only). None when the installer is not an Inno
    Setup installer gitfurl can read, or asks nothing. Raises ValueError for an answer the
    setup does not have, ChoicesStopped at the end of input."""
    got = load(installer)
    if got is None:
        if answers:
            raise ValueError("Setup's choices (--language, --type, --components, --tasks) "
                             "need an Inno Setup installer whose setup data gitfurl can read")
        return None
    setup, ch = got
    if not ch.asks:
        if answers:
            raise ValueError("this setup asks for no language, setup type, component or task")
        return None
    notes: list[str] = []
    from gitfurl.inno import choice_effects

    if mode in SECTIONS and not any(sec in SECTIONS[mode] for sec, _n, _k in
                                    choice_effects(setup, ch.defaults())):
        notes.append("no " + " or ".join(SECTIONS[mode]) + " entry names a component, task "
                     "or language, so the answers change nothing here")
    if answers:
        choice, how = ch.answer(**answers), "given on the command line"
        if mode not in APPLIED:
            notes.append("the answers given are not used on this route")
    elif mode not in APPLIED:
        choice, how = ch.defaults(), "shown"
    elif notes:
        choice, how = ch.answer(), "not asked"
    elif dry_run:
        choice, how = ch.answer(), "the script's defaults; a real run asks"
    elif yes or not tty:
        choice = ch.answer()
        how = "the script's defaults, taken without asking: " + (
            "--yes" if yes else "no terminal to ask on")
    else:
        choice, how = ask(ch, input_fn, out), "asked"
    return Picked(ch, choice, mode, how, setup, notes)


def native_notes(picked: Picked, archives: list[str]) -> list[str]:
    """The honest notes for the native route's FreeArc archives: their contents are chosen by
    the script's [Code], so they cannot be filtered by component."""
    ch = picked.choices
    if not ch.components or not archives:
        return []
    tie = _ties(ch, archives, picked.setup.code.strings if picked.setup is not None else [])
    return [(f"the components look tied to the archives ({tie}); " if tie else
             "the script's [Code] picks the archives; ") + FREEARC_NOTE]


def _base(name: str) -> str:
    stem = name.casefold().rsplit(".", 1)[0]
    return re.sub(r"[-_. ]?(?:part)?\d{1,3}$", "", stem).strip(" -_.")


def _ties(ch, archives: list[str], strings: list[str]) -> str:
    """A short text when component names look tied to archive names, else ''."""
    found = []
    for c in ch.components:
        leaf = c.name.rsplit("\\", 1)[-1].casefold()
        if len(leaf) < 3:
            continue
        for a in archives:
            b = _base(a)
            if len(b) >= 3 and (leaf in b or b in leaf):
                found.append(f"component {clean(c.name, 40)} and {clean(a, 60)}")
                break
    if found:
        return "; ".join(found[:3]) + (" ..." if len(found) > 3 else "")
    low = [s.casefold() for s in strings]
    named_comp = [c.name for c in ch.components if c.key in low]
    named_arc = [a for a in archives if any(a.casefold() in s for s in low)]
    if named_comp and named_arc:
        return ("its [Code] names components (" + ", ".join(clean(n, 40) for n in named_comp[:3])
                + ") and archives (" + ", ".join(clean(n, 60) for n in named_arc[:3]) + ")")
    return ""


# --- the window ------------------------------------------------------------------------------

@dataclass
class Row:
    """One line of the window's card: a heading, a radio group or a checkbox."""

    kind: str  # "heading", "radio", "check" or "text"
    key: str  # "language", "type", "c:<name>", "t:<name>", "g:c:<parent>", "g:t:<parent>"
    text: str
    value: object = None  # radio: the chosen option's value ("" for none); check: bool
    options: list = field(default_factory=list)  # radio: [(value, label)]
    indent: int = 0
    enabled: bool = True


def window_rows(ch, choice=None) -> list[Row]:
    """The rows of the window's card for these answers (default: the script's defaults)."""
    c = choice or ch.defaults()
    rows: list[Row] = []
    if len(ch.languages) > 1:
        rows.append(Row("radio", "language", "Language", c.language,
                        [(x.name.casefold(), clean(x.display or x.name, 40))
                         for x in ch.languages]))
    if ch.types:
        rows.append(Row("radio", "type", "Setup type", c.setup_type,
                        [(t.name.casefold(), clean(t.description or t.name, 60))
                         for t in ch.types]))
    for what, items, on, shown in (
            ("c", ch.components, c.components,
             {i.key for i in ch.shown_components(c.language)}),
            ("t", ch.tasks, c.tasks, {i.key for i in ch.shown_tasks(c.components, c.language)})):
        if not items:
            continue
        free = what == "t" or ch.components_free(c.setup_type)
        rows.append(Row("heading", f"h:{what}", "Components" + (
            "" if free else " (set by the setup type; choose a custom type to pick them)")
            if what == "c" else "Tasks"))
        groups: set[str] = set()
        heading = ""
        for i in items:
            ok = free and i.key in shown and not i.fixed
            if i.group and i.group != heading:
                heading = i.group
                rows.append(Row("text", f"g:{i.key}", clean(i.group, 80)))
            if i.exclusive:
                if i.parent in groups:
                    continue
                groups.add(i.parent)
                members = [m for m in items if m.exclusive and m.parent == i.parent]
                value = next((m.key for m in members if m.key in on), "")
                rows.append(Row("radio", f"g:{what}:{i.parent}", "Choose one", value,
                                [(m.key, _label(m)) for m in members] + [("", "none")],
                                i.level, ok))
                continue
            rows.append(Row("check", f"{what}:{i.key}", _label(i)
                            + (" (fixed)" if i.fixed else ""), i.key in on, [], i.level, ok))
    for p in ch.postinstall:
        rows.append(Row("text", "p", f"After the install, Setup offers: "
                        f"{clean(p.description, 80)}"))
    rows.extend(Row("text", "u", f"Not seen: {u}") for u in ch.unseen)
    return rows


def window_choice(ch, values: dict):
    """The Choice for the card's values ({row key: value}, as window_rows names them)."""
    base = ch.defaults()
    lang = values.get("language", base.language)
    stype = values.get("type", base.setup_type)

    def answers(what: str, items) -> dict[str, bool]:
        out: dict[str, bool] = {}
        for i in items:
            if i.exclusive:
                v = values.get(f"g:{what}:{i.parent}")
                if v is not None:
                    out[i.key] = v == i.key
            elif f"{what}:{i.key}" in values:
                out[i.key] = bool(values[f"{what}:{i.key}"])
        return out

    return ch.resolve(lang, stype, answers("c", ch.components), answers("t", ch.tasks))


def window_mode(native: bool, setup_entries: bool, unattended: bool) -> str:
    """How the window's answers are used for the route it takes."""
    if native:
        return MODE_ENTRIES if setup_entries else MODE_NATIVE
    return MODE_SWITCHES if unattended else MODE_WIZARD
