"""Native-plan advice and warnings with synthetic setups and archives; no Wine."""
from types import SimpleNamespace

try:
    from test_namelinks import Base
except ImportError:
    from tests.test_namelinks import Base

from gitfurl import timeline
from gitfurl.detect import Evidence, describe, detect, evidence_lines
from gitfurl.namelinks import LinkProposal, propose_for
from gitfurl.route import NativeInstallPlan, choose_route


class NativeAdviceTests(Base):
    def choice(self, links=True):
        inst = self.make()
        det = detect(inst)
        choice = choose_route(inst, "p1", det, env=self.env,
                              links=propose_for(inst) if links else None)
        return inst, det, choice

    def test_valid_name_mapping_is_suggested_without_enabling_it(self):
        inst, _det, choice = self.choice(links=False)
        self.assertEqual(choice.route, "wine")
        self.assertIsNone(choice.links)
        self.assertIn("--link-names", choice.line())
        self.assertIn("review", choice.line())
        self.assertFalse((inst.parent / "fg-01.bin").exists())

    def test_unmatched_series_does_not_suggest_link_names(self):
        inst = self.make(archives=("other-01.bin",))
        choice = choose_route(inst, "p1", detect(inst), env=self.env)
        self.assertEqual(choice.route, "wine")
        self.assertNotIn("--link-names", choice.line())

    def test_native_detection_shows_mapped_names_and_remaining_missing_name(self):
        _inst, det, choice = self.choice()
        text = describe(det, choice)
        self.assertIn("supplied by other-01.bin", text)
        self.assertIn("supplied by other-02.bin", text)
        self.assertNotIn("silent run is likely to stop", text)
        self.assertIn("fg.inf: missing", text)
        # Detection is a reusable observation, not mutated by a native rendering.
        self.assertIn("silent run is likely to stop", describe(det))

    def test_native_timeline_does_not_repeat_setup_only_missing_warnings(self):
        _inst, det, choice = self.choice()
        tl = timeline.Timeline()
        timeline.plan_entries(tl, det, choice, self.prefix, False, True)
        self.assertNotIn("silent run is likely to stop", tl.text())
        self.assertNotIn("fg-01.bin", "\n".join(e.what for e in tl.entries()
                                                if e.what.startswith("Warning:")))
        wine = choose_route(self.setup, "p1", det, env=self.env, requested="wine")
        other = timeline.Timeline()
        timeline.plan_entries(other, det, wine, self.prefix, False, True)
        self.assertIn("silent run is likely to stop", other.text())

    def test_shortened_mapped_names_are_not_identified_by_their_shared_display(self):
        names = tuple("a" * 85 + suffix for suffix in ("-01.bin", "-02.bin"))
        inst = self.make(code=tuple("{src}\\" + n for n in names))
        det = detect(inst)
        links = propose_for(inst)
        self.assertEqual(len(links.links), 2)
        choice = choose_route(inst, "p1", det, env=self.env, links=links)
        text = describe(det, choice)
        sources = [line for line in text.splitlines() if "file beside the installer:" in line]
        self.assertEqual(len(sources), 2)
        self.assertTrue(all(": missing" in line for line in sources))
        self.assertNotIn("supplied by", "\n".join(sources))
        self.assertIn("shortened or ambiguous source names", text)

    def test_shortened_unmapped_name_is_not_claimed_as_supplied(self):
        names = tuple("a" * 85 + suffix for suffix in ("-01.bin", ".inf"))
        inst = self.make(code=tuple("{src}\\" + n for n in names),
                         archives=("other-01.bin",))
        det = detect(inst)
        links = propose_for(inst)
        self.assertEqual(len(links.links), 1)
        self.assertEqual(links.unmatched, (names[1],))
        choice = choose_route(inst, "p1", det, env=self.env, links=links)
        text = describe(det, choice)
        sources = [line for line in text.splitlines() if "file beside the installer:" in line]
        self.assertEqual(len(sources), 2)
        self.assertTrue(all(": missing" in line for line in sources))
        self.assertNotIn("supplied by", "\n".join(sources))

    def test_cleaned_unmapped_name_cannot_impersonate_a_mapping(self):
        # One exact mapped spelling and an unmatched control character clean to the same
        # display. The display cannot identify which observation belongs to the mapping.
        raw = "a\x01-01.bin"
        lines = [f"file beside the installer: {name}: missing; named by [Code] string"
                 for name in ("a?-01.bin", "a?-01.bin")]
        ev = Evidence("Inno Setup", "likely", "", "", lines)
        choice = SimpleNamespace(route="native", links=LinkProposal(
            links=(("a?-01.bin", "other-01.bin"),), unmatched=(raw,)))
        text = "\n".join(evidence_lines(ev, choice))
        self.assertNotIn("supplied by", text)
        self.assertEqual(text.count(": missing; named by"), 2)

    def test_native_plan_does_not_deny_the_entries_it_will_add(self):
        inst, _det, choice = self.choice()
        plan = NativeInstallPlan(inst, "p1", self.prefix, choice, False,
                                 setup_entries=SimpleNamespace(describe=lambda: [
                                     "registry: no value to write"]))
        text = "\n".join(plan.describe())
        self.assertNotIn("no registry entries", text)
        self.assertNotIn("no shortcuts", text)
        self.assertIn("only after a complete unpack: add the registry entries", text)
        self.assertIn("no registry entries", "\n".join(choice.extract.describe()))
