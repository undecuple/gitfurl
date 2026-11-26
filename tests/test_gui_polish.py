"""Tests for the window's polish: contrast, the checklist, the footer, keyboard use, focus rings,
the sticky action bar and resizing.

Pure functions are tested directly. The window tests start gitfurl under `xvfb-run -a` in a
subprocess, drive it through `gitfurl.gui.main(driver=...)` and read back one JSON line. They
need no Wine, installer or network (the Wine path is faked) and no fixed sleeps: events are
pumped with root.update() until a condition holds, with a cap. They are skipped when
xvfb-run or tkinter is missing. They do not test a real display, HiDPI or any screen reader.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from gitfurl import theme
from gitfurl.gui import (
    create_reason,
    env_summary,
    key_hints,
    plan_checklist,
    run_reason,
    run_state_text,
)

SRC = str(Path(__file__).resolve().parent.parent / "src")


class ContrastTests(unittest.TestCase):
    def test_brick_edges(self):
        self.assertEqual(theme.mix("#000000", "#FFFFFF", 0.5), "#808080")
        self.assertEqual(theme.mix(theme.ORANGE, "#123456", 0), theme.ORANGE)
        for c in (theme.ORANGE, theme.LINE, theme.PANEL):
            # the lit edge is lighter and the shaded edge darker than the brick itself
            self.assertGreater(theme.luminance(theme.lit(c)), theme.luminance(c))
            self.assertLess(theme.luminance(theme.shade(c)), theme.luminance(c))
        self.assertEqual(theme.RADIUS, 0)
        self.assertGreater(theme.STUD_H, 0)

    def test_known_ratios(self):
        self.assertAlmostEqual(theme.contrast_ratio("#000000", "#FFFFFF"), 21.0, places=6)
        self.assertAlmostEqual(theme.contrast_ratio("#777777", "#777777"), 1.0, places=6)
        # symmetric, and the well-known grey-on-white value (#767676 is just over 4.5)
        self.assertAlmostEqual(theme.contrast_ratio("#767676", "#FFFFFF"), 4.54, places=2)
        self.assertEqual(theme.contrast_ratio("#123456", "#ABCDEF"),
                         theme.contrast_ratio("#ABCDEF", "#123456"))

    def test_every_text_colour_reaches_aa(self):
        for name, fg, bg in theme.TEXT_PAIRS:
            with self.subTest(name):
                self.assertGreaterEqual(theme.contrast_ratio(fg, bg), 4.5, f"{name} {fg} on {bg}")

    def test_focus_ring_is_visible_on_every_surface(self):
        for bg in theme.FOCUS_SURFACES:
            with self.subTest(bg):
                self.assertGreaterEqual(theme.contrast_ratio(theme.FOCUS_RING, bg), 3.0)
        self.assertGreaterEqual(theme.contrast_ratio(theme.FOCUS_RING_ON_ACCENT, theme.ORANGE), 2.5)
        # the ring on the orange button has to differ from the button fill itself
        self.assertNotEqual(theme.FOCUS_RING_ON_ACCENT, theme.ORANGE)

    def test_one_scale_each(self):
        self.assertEqual(sorted(theme.SPACE.values()), list(theme.SPACE.values()))
        self.assertEqual(sorted(theme.TYPE.values()), list(theme.TYPE.values()))
        self.assertIsInstance(theme.RADIUS, int)


class ChecklistTests(unittest.TestCase):
    def rows(self, **kw):
        args = {"installer_error": None, "prefix_error": None, "prefix_exists": True,
                "will_create": True, "runtime": "wine", "runtime_found": True}
        args.update(kw)
        return plan_checklist(**args)

    def test_all_good(self):
        rows = self.rows()
        self.assertTrue(all(ok for _k, ok, _t in rows))
        self.assertEqual([k for k, _o, _t in rows], ["installer", "runtime", "prefix"])
        self.assertEqual(run_reason(rows, busy=False), "")

    def test_each_missing_thing_is_named_in_words(self):
        rows = self.rows(installer_error="Choose an installer file.", runtime_found=False,
                         prefix_exists=False, will_create=False)
        self.assertEqual([ok for _k, ok, _t in rows], [False, False, False])
        text = " | ".join(t for _k, _o, t in rows)
        self.assertIn("Choose an installer file.", text)
        self.assertIn("Wine not found on PATH", text)
        self.assertIn("Prefix does not exist", text)
        reason = run_reason(rows, busy=False)
        self.assertTrue(reason.startswith("Run is off: Choose an installer file."))
        self.assertIn("2 more", reason)

    def test_prefix_is_fine_when_it_will_be_created(self):
        rows = self.rows(prefix_exists=False, will_create=True)
        self.assertTrue({k: ok for k, ok, _t in rows}["prefix"])
        self.assertIn("will be created", rows[2][2])

    def test_proton_names_umu_run(self):
        rows = self.rows(runtime="proton", runtime_found=False)
        self.assertEqual(rows[1][2], "umu-run not found on PATH")

    def test_plan_error_only_shows_when_the_rest_passes(self):
        self.assertEqual(len(self.rows(plan_error="needs a Proton folder")), 4)
        self.assertFalse(self.rows(plan_error="needs a Proton folder")[3][1])
        self.assertEqual(len(self.rows(runtime_found=False, plan_error="x")), 3)

    def test_invalid_prefix_name(self):
        rows = self.rows(prefix_error="invalid prefix name", prefix_exists=False)
        self.assertEqual(rows[2], ("prefix", False, "invalid prefix name"))

    def test_busy_turns_everything_off_with_words(self):
        self.assertIn("already running", run_reason(self.rows(), busy=True))
        self.assertIn("already running", create_reason(None, False, busy=True))

    def test_create_reasons(self):
        self.assertEqual(create_reason(None, False, False), "")
        self.assertIn("already exists", create_reason(None, True, False))
        self.assertIn("invalid", create_reason("invalid name", False, False))


class FooterTextTests(unittest.TestCase):
    def test_environment_summary_says_ok_or_missing(self):
        self.assertEqual(env_summary(True, False), "Wine: ok    umu-run: missing")
        self.assertEqual(env_summary(False, True), "Wine: missing    umu-run: ok")

    def test_hints_name_the_keys(self):
        for page in ("library", "details", "settings"):
            self.assertIn("Tab: move", key_hints(page))
            self.assertIn("Enter/Space", key_hints(page))
        self.assertIn("Esc: back", key_hints("details"))
        self.assertIn("Up/Down", key_hints("library"))
        self.assertNotIn("Esc", key_hints("settings"))

    def test_run_state(self):
        self.assertEqual(run_state_text(None, False), "Idle")
        self.assertEqual(run_state_text("install", False), "Installer running")
        self.assertEqual(run_state_text("prefix", False), "Creating prefix")
        self.assertEqual(run_state_text("install", True), "Stopping...")
        self.assertEqual(run_state_text(None, True), "Idle")


PRELUDE = textwrap.dedent('''
    import json, sys, os
    from gitfurl import widgets as w
    from gitfurl.gui import main

    def pump_until(root, cond, cap=400):
        for _ in range(cap):
            root.update()
            if cond():
                return True
        return False

    def settle(root, n=15):
        for _ in range(n):
            root.update()

    def key(root, name, target=None):
        (target or root.focus_get() or root).event_generate(name)
        settle(root, 4)

    def ring_items(c):
        out = []
        for i in c.find_all():
            if c.type(i) == "polygon" and c.itemcget(i, "outline") in (w.FOCUS_RING,
                    w.FOCUS_RING_ON_ACCENT) and float(c.itemcget(i, "width")) >= 2:
                out.append(i)
        return out

    def descendants(widget):
        for ch in widget.winfo_children():
            yield ch
            yield from descendants(ch)

    result = {}

    def finish(root):
        print("RESULT " + json.dumps(result))
        root.destroy()
''')


def run_window(body: str, start: str = "library", prefixes=("alpha", "beta")):
    """Run `body` (the driver) in the window under xvfb-run. Returns the JSON it reported."""
    if shutil.which("xvfb-run") is None:
        raise unittest.SkipTest("xvfb-run not available")
    try:
        import tkinter  # noqa: F401
    except ImportError:
        raise unittest.SkipTest("tkinter not installed") from None
    script = PRELUDE + "\ndef drive(root, ui):\n" + textwrap.indent(textwrap.dedent(body), "    ")
    script += f"\nsys.exit(main(start={start!r}, driver=drive))\n"
    with tempfile.TemporaryDirectory() as home:
        for name in prefixes:
            (Path(home) / "d" / "gitfurl" / "prefixes" / name).mkdir(parents=True)
        installer = Path(home) / "setup.exe"
        installer.write_bytes(b"MZ not an installer, a test file")
        env = {**os.environ, "PYTHONPATH": SRC, "HOME": home, "XDG_DATA_HOME": home + "/d",
               "XDG_STATE_HOME": home + "/s", "TEST_INSTALLER": str(installer)}
        proc = subprocess.run(["xvfb-run", "-a", "-s", "-screen 0 1400x900x24", sys.executable, "-c",
                               script], env=env, capture_output=True, text=True, check=False,
                              timeout=120)
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT ")]
    if proc.returncode != 0 or not lines:
        raise AssertionError(f"window driver failed ({proc.returncode}):\n{proc.stderr[-2000:]}\n"
                             f"{proc.stdout[-500:]}")
    return json.loads(lines[-1][7:])


class WindowPolishTests(unittest.TestCase):
    def test_library_arrows_and_enter(self):
        r = run_window('''
            root.focus_force()
            assert pump_until(root, lambda: ui["pages"]["library"].winfo_ismapped())
            cards = ui["cards"]
            result["count"] = len(cards)
            key(root, "<Down>", root)
            result["first"] = cards.index(root.focus_get()) if root.focus_get() in cards else -1
            key(root, "<Down>")
            key(root, "<Down>")
            key(root, "<Down>")  # one past the end stays on the last card
            result["last"] = cards.index(root.focus_get()) if root.focus_get() in cards else -1
            key(root, "<Up>")
            result["up"] = cards.index(root.focus_get()) if root.focus_get() in cards else -1
            result["ring_on_focus"] = len(ring_items(root.focus_get())) > 0
            result["ring_elsewhere"] = sum(1 for c in cards if c is not root.focus_get()
                                           and ring_items(c))
            chosen = ui["cards"].index(root.focus_get())
            key(root, "<Return>")
            result["page_after_enter"] = ui["nav_state"]["page"]
            result["prefix"] = ui["prefix_var"].get()
            result["chosen"] = chosen
            finish(root)
        ''')
        self.assertEqual(r["count"], 3)  # alpha, beta and the always-listed "default"
        self.assertEqual((r["first"], r["last"], r["up"]), (0, 2, 1))
        self.assertTrue(r["ring_on_focus"])
        self.assertEqual(r["ring_elsewhere"], 0)
        self.assertEqual(r["page_after_enter"], "details")
        self.assertEqual(r["prefix"], "beta")

    def test_escape_goes_back_only_from_details(self):
        r = run_window('''
            root.focus_force()
            settle(root)
            ui["show"]("settings")
            key(root, "<Escape>", root)
            result["settings"] = ui["nav_state"]["page"]
            ui["show"]("details")
            settle(root)
            key(root, "<Escape>", root)
            pump_until(root, lambda: ui["nav_state"]["page"] == "library")
            result["details"] = ui["nav_state"]["page"]
            finish(root)
        ''', start="library")
        self.assertEqual(r["settings"], "settings")  # Esc is only specified for the details page
        self.assertEqual(r["details"], "library")

    def test_escape_does_not_leave_the_details_page_while_a_run_is_busy(self):
        r = run_window('''
            root.focus_force()
            ui["show"]("details")
            settle(root)
            ui["busy"]["on"] = True            # what start_busy sets for a run
            ui["stop_btn"].state(["!disabled"])
            key(root, "<Escape>", root)
            for _ in range(20):
                root.update()
            result["busy_page"] = ui["nav_state"]["page"]
            result["stop_enabled"] = ui["stop_btn"]._state == "normal"
            ui["busy"]["on"] = False           # the run ended: Esc works again
            key(root, "<Escape>", root)
            pump_until(root, lambda: ui["nav_state"]["page"] == "library")
            result["after_page"] = ui["nav_state"]["page"]
            finish(root)
        ''', start="library")
        self.assertEqual(r["busy_page"], "details")  # the Stop button stays reachable
        self.assertTrue(r["stop_enabled"])
        self.assertEqual(r["after_page"], "library")

    def test_space_and_return_activate_and_focus_ring_shows(self):
        r = run_window('''
            root.focus_force()
            ui["show"]("details")
            settle(root)
            switches = [x for x in descendants(ui["pages"]["details"]) if isinstance(x, w.Switch)]
            sw = switches[0]
            sw.focus_force()
            assert pump_until(root, lambda: sw._focused)
            result["ring_on"] = len(ring_items(sw)) > 0
            before = ui["create_var"].get()
            key(root, "<space>")
            result["space_flipped"] = ui["create_var"].get() != before
            key(root, "<Return>")
            result["return_flipped_back"] = ui["create_var"].get() == before
            root.focus_force()
            ui["nav_items"]["library"].focus_force()
            assert pump_until(root, lambda: not sw._focused)
            result["ring_off"] = len(ring_items(sw)) == 0
            finish(root)
        ''')
        self.assertTrue(r["ring_on"])
        self.assertTrue(r["space_flipped"])
        self.assertTrue(r["return_flipped_back"])
        self.assertTrue(r["ring_off"])

    def test_every_custom_widget_draws_a_ring_when_focused(self):
        r = run_window('''
            root.focus_force()
            settle(root)
            seen = {}
            for page in ("library", "details", "settings"):
                ui["show"](page)
                settle(root)
                extra = list(ui["nav_items"].values()) if page == "library" else []
                for x in list(descendants(ui["pages"][page])) + extra:
                    kind = type(x).__name__
                    custom = isinstance(x, (w.PillButton, w.Switch)) or (
                        isinstance(x, w.RoundCard) and x.cget("takefocus") in ("1", 1)) or (
                        x in extra)
                    if not custom or str(x.cget("takefocus")) in ("0", "") or not x.winfo_viewable():
                        continue
                    kind = "NavItem" if x in extra else kind
                    x.focus_force()
                    ok = pump_until(root, lambda: getattr(x, "_focused", False))
                    seen.setdefault(kind, []).append([ok, len(ring_items(x)) > 0])
            result["seen"] = seen
            finish(root)
        ''')
        seen = r["seen"]
        for kind in ("PillButton", "Switch", "RoundCard", "NavItem"):
            self.assertIn(kind, seen)
            for ok, ring in seen[kind]:
                self.assertTrue(ok and ring, kind)

    def test_tab_order_follows_the_screen_on_details(self):
        r = run_window('''
            root.focus_force()
            ui["wine_path"]["value"] = "/nonexistent/wine"
            ui["show"]("details")
            settle(root)
            det_in = ui["inners"]["details"]
            first = ui["nav_items"]["library"]
            first.focus_force()
            pump_until(root, lambda: root.focus_get() is first)
            seq, ys = [], []
            for _ in range(80):
                key(root, "<Tab>")
                f = root.focus_get()
                if f is first or f is None:
                    break
                seq.append(str(f))
                if str(f).startswith(str(det_in)):
                    ys.append([f.winfo_rooty() - det_in.winfo_rooty(),
                               f.winfo_rootx() - det_in.winfo_rootx()])
                elif str(f).startswith(str(ui["action"])):
                    ys.append([10**6, f.winfo_rootx()])
            result["n"] = len(seq)
            result["ys"] = ys
            result["wrapped"] = f is first
            result["has_action"] = any(s.startswith(str(ui["action"])) for s in seq)
            finish(root)
        ''')
        self.assertTrue(r["wrapped"], "Tab did not come back to the start (a trap?)")
        self.assertGreater(r["n"], 8)
        self.assertTrue(r["has_action"])
        self.assertEqual(r["ys"], sorted(r["ys"]), "Tab order differs from the screen order")

    def test_footer_on_every_page_and_follows_state(self):
        r = run_window('''
            root.focus_force()
            ui["wine_path"].update(value="/fake/wine", umu=None)
            pages = {}
            for page in ("library", "details", "settings"):
                ui["show"](page)
                settle(root)
                ui["refresh_footer"]()
                settle(root, 3)
                bottom = ui["foot_env"].winfo_rooty() + ui["foot_env"].winfo_height()
                pages[page] = dict(
                    env=ui["foot_env"].cget("text"), hints=ui["foot_hints"].cget("text"),
                    state=ui["foot_state"].cget("text"),
                    visible=ui["foot_env"].winfo_viewable() and bottom <= root.winfo_rooty()
                    + root.winfo_height())
            result["pages"] = pages
            ui["run"].update(kind="install", stopping=False); ui["refresh_footer"]()
            result["running"] = ui["foot_state"].cget("text")
            ui["run"].update(stopping=True); ui["refresh_footer"]()
            result["stopping"] = ui["foot_state"].cget("text")
            ui["run"].update(kind=None, stopping=False); ui["refresh_footer"]()
            result["idle"] = ui["foot_state"].cget("text")
            finish(root)
        ''')
        for page, info in r["pages"].items():
            self.assertTrue(info["visible"], page)
            self.assertEqual(info["env"], "Wine: ok    umu-run: missing")
            self.assertEqual(info["state"], "Idle")
            self.assertEqual(info["hints"], key_hints(page))
        self.assertEqual((r["running"], r["stopping"], r["idle"]),
                         ("Installer running", "Stopping...", "Idle"))

    def test_banner_checklist_and_reasons(self):
        r = run_window('''
            root.focus_force()
            ui["memory_var"].set(False)  # the memory limit has its own test (test_memlimit)
            ui["show"]("details")
            ui["wine_path"].update(value=None, umu=None)
            ui["set_banners"](True)
            ui["refresh_plan"]()
            settle(root)
            rows = ui["check_rows"]
            result["banner_details"] = ui["banners"][1][1].winfo_viewable()
            ui["show"]("library"); settle(root)
            result["banner_library"] = ui["banners"][0][1].winfo_viewable()
            ui["show"]("details"); settle(root)
            result["text_off"] = [rows[k][2].cget("text") for k in ("installer", "runtime", "prefix")]
            result["run_off"] = ui["run_btn"]._state
            result["reason_off"] = ui["reason_var"].get()
            ui["installer_var"].set(os.environ["TEST_INSTALLER"])
            ui["wine_path"]["value"] = "/fake/wine"
            ui["set_banners"](False)
            ui["refresh_plan"](); settle(root)
            result["banner_gone"] = not ui["banners"][1][1].winfo_viewable()
            result["slot_height"] = ui["banners"][1][0].winfo_height()
            result["run_on"] = ui["run_btn"]._state
            result["reason_on"] = ui["reason_var"].get()
            ui["create_var"].set(False); settle(root)
            result["run_noprefix"] = ui["run_btn"]._state
            result["reason_noprefix"] = ui["reason_var"].get()
            result["create_state"] = ui["create_btn"]._state
            finish(root)
        ''', start="details")
        self.assertTrue(r["banner_details"])
        self.assertTrue(r["banner_library"])
        self.assertTrue(r["banner_gone"])
        self.assertLessEqual(r["slot_height"], 2)  # no blank gap where the banner was
        self.assertEqual(r["text_off"][0], "Choose an installer file.")
        self.assertEqual(r["text_off"][1], "Wine not found on PATH")
        self.assertEqual(r["run_off"], "disabled")
        self.assertIn("Run is off: Choose an installer file.", r["reason_off"])
        self.assertEqual((r["run_on"], r["reason_on"]), ("normal", ""))
        self.assertEqual(r["run_noprefix"], "disabled")
        self.assertIn("Prefix does not exist", r["reason_noprefix"])
        self.assertEqual(r["create_state"], "normal")

    def test_resize_wheel_and_sticky_bar_at_minimum_size(self):
        r = run_window('''
            root.focus_force()
            ui["wine_path"].update(value=None, umu=None)
            ui["set_banners"](True)
            result["minsize"] = list(root.minsize())
            result["resizable"] = list(root.resizable())
            root.geometry("900x620")
            ui["show"]("details")
            pump_until(root, lambda: root.winfo_width() == 900 and root.winfo_height() == 620)
            settle(root, 40)
            cv, inner = ui["canvases"]["details"], ui["inners"]["details"]
            result["taller"] = inner.winfo_height() > cv.winfo_height()
            bar = ui["action"]
            win_bottom = root.winfo_rooty() + root.winfo_height()
            result["bar_visible"] = bar.winfo_viewable() and (
                bar.winfo_rooty() + bar.winfo_height() <= win_bottom)
            result["run_visible"] = ui["run_btn"].winfo_viewable() and (
                ui["run_btn"].winfo_rootx() + ui["run_btn"].winfo_width()
                <= root.winfo_rootx() + root.winfo_width())
            # the bar sits above the footer and below the scrolling area
            result["bar_below_content"] = bar.winfo_rooty() >= cv.winfo_rooty() + cv.winfo_height()
            clipped = []
            for page in ("library", "details", "settings"):
                ui["show"](page)
                settle(root, 20)
                c, inn = ui["canvases"][page], ui["inners"][page]
                limit = c.winfo_rootx() + c.winfo_width() + 1
                for x in descendants(inn):
                    if x.winfo_viewable() and x.winfo_width() > 1 and (
                            x.winfo_rootx() + x.winfo_width() > limit):
                        clipped.append([page, str(x)])
            result["clipped"] = clipped
            ui["show"]("details")
            settle(root, 20)
            cv.yview_moveto(0)
            cv.event_generate("<Button-5>", x=20, y=20)
            result["wheel_down"] = pump_until(root, lambda: cv.yview()[0] > 0)
            cv.event_generate("<Button-4>", x=20, y=20)
            cv.event_generate("<Button-4>", x=20, y=20)
            result["wheel_up"] = pump_until(root, lambda: cv.yview()[0] == 0)
            # Tab to a control far down the page scrolls it into view
            deep = [x for x in descendants(inner) if isinstance(x, w.PillButton)
                    and x.winfo_rooty() - inner.winfo_rooty() > cv.winfo_height()][-1]
            deep.focus_force()
            result["revealed"] = pump_until(root, lambda: (
                deep.winfo_rooty() + deep.winfo_height() <= cv.winfo_rooty() + cv.winfo_height()))
            # growing keeps the content filling the width
            root.geometry("1300x860")
            pump_until(root, lambda: root.winfo_width() == 1300)
            settle(root, 30)
            result["fills"] = abs(inner.winfo_width() - cv.winfo_width()) <= 1
            finish(root)
        ''', start="details")
        self.assertEqual(r["minsize"], [900, 620])
        self.assertEqual(r["resizable"], [True, True])
        self.assertTrue(r["taller"])
        self.assertTrue(r["bar_visible"])
        self.assertTrue(r["run_visible"])
        self.assertTrue(r["bar_below_content"])
        self.assertEqual(r["clipped"], [])
        self.assertTrue(r["wheel_down"])
        self.assertTrue(r["wheel_up"])
        self.assertTrue(r["revealed"])
        self.assertTrue(r["fills"])


if __name__ == "__main__":
    unittest.main()
