"""Review of 14 Nov, roadmap V4: the chat log read every message aloud, some theme colours were too faint, ⌘K was
named on Windows, two Settings sections were both "General", and the phone said the same thing three times."""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import crewchat  # noqa: E402

PAGE = crewchat.CHAT_PAGE
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def luminance(colour):
    channels = [int(colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    r, g, b = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    la, lb = luminance(a), luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def themes():
    found = {}
    for m in re.finditer(r'(?:^|\n)(?::root, )?\[data-theme="([a-z-]+)"\] \{(.*?)\n\}', PAGE, re.S):
        found.setdefault(m.group(1), {}).update(dict(re.findall(r"(--[a-z0-9-]+):\s*([^;]+);", m.group(2))))
    return found


class ScreenReader(unittest.TestCase):
    def test_the_chat_log_does_not_read_every_message_aloud(self):
        log = re.search(r'<div id="log"[^>]*>', PAGE).group(0)
        self.assertIn('aria-live="off"', log)
        self.assertRegex(PAGE, r'<div id="announce" class="sr" role="status" aria-live="polite"')

    def test_messages_to_you_and_new_needs_are_announced(self):
        self.assertIn('(m.to === "Owner" || /@owner\\b/i.test(m.text || ""))', PAGE)  # @you in a crew message too
        self.assertIn('announce(fresh.length === 1 ? "Needs you: "', PAGE)


class Contrast(unittest.TestCase):
    def test_every_theme_has_dots_and_marks_that_can_be_seen(self):
        """--off draws offline dots, swatches and the To do column: 3:1 against the panel it sits on (WCAG 1.4.11)."""
        checked = 0
        for name, v in themes().items():
            off, panel = v.get("--off", "").strip(), v.get("--panel", "").strip()
            if HEX.match(off) and HEX.match(panel):
                checked += 1
                self.assertGreaterEqual(contrast(off, panel), 3, name)
            bg = v.get("--bg", "").strip()
            if HEX.match(off) and HEX.match(bg) and (luminance(bg) > 0.6 or luminance(bg) < 0.05):
                self.assertGreaterEqual(contrast(off, bg), 3, name + " on its background")
        self.assertGreaterEqual(checked, 8)

    def test_the_focus_ring_can_be_seen_in_every_theme(self):
        """The keyboard focus ring: 3:1 against the cards and the background it sits on (WCAG 2.4.11, 1.4.11)."""
        self.assertIn(":focus-visible { outline: 2px solid var(--focus, var(--accent));", PAGE)
        checked = 0
        for name, v in themes().items():
            ring = v.get("--focus", "var(--accent)").strip()
            ring = v.get("--accent", "").strip() if ring == "var(--accent)" else ring
            for under in ("--panel", "--bg"):
                if HEX.match(ring) and HEX.match(v.get(under, "").strip()):
                    checked += 1
                    self.assertGreaterEqual(contrast(ring, v[under].strip()), 3, name + " ring on " + under)
        self.assertGreaterEqual(checked, 18)
        # On the rail and the Settings menu the ring is the rail's link colour (round-2 re-review: Sage's dark rail).
        self.assertIn("--focus: var(--rail-link, var(--accent)); }", PAGE)
        for name, v in themes().items():
            link, rail = v.get("--rail-link", "").strip(), v.get("--rail-bg", "").strip()
            if HEX.match(link) and HEX.match(rail):
                self.assertGreaterEqual(contrast(link, rail), 3, name + " ring on the rail")
        # Lagoon's cards are white on a dark page: a white halo outside the green ring shows on the dark parts.
        self.assertEqual(themes()["lagoon"]["--focus-halo"].strip(), "#FFFFFF")
        # Only on what is clicked, not on text boxes or the resize handles, whose own ring it would hide (round 3).
        halo = re.search(r'\[data-theme="lagoon"\] (.*?):focus-visible \{ box-shadow: 0 0 0 6px var\(--focus-halo\); \}', PAGE)
        self.assertTrue(halo)
        self.assertIn("button", halo.group(1))
        self.assertIn(":not(.grip)", halo.group(1))
        self.assertNotIn("input", halo.group(1))
        self.assertIn(".namefield input.nm:focus-visible { outline: 2px solid", PAGE)
        banner = PAGE.split('<div id="banner"')[1].split("</div>")[0]
        self.assertIn("<code>crewchat start .</code>", banner)  # a single machine whose server stopped

    def test_warning_text_reads_in_the_classic_light_theme(self):
        v = themes()["classic-light"]
        self.assertGreaterEqual(contrast(v["--warn"].strip(), v["--panel"].strip()), 4.5)


class FirstRun(unittest.TestCase):
    def test_an_empty_chat_leads_to_new_agent(self):
        """After the welcome steps the chat is empty: New agent is the one filled button, has focus, and help is a link away."""
        body = PAGE[PAGE.index("function welcome() {"):]
        body = body[:body.index("\n}\n")]
        self.assertIn('const start = actButton(acts, "New agent"', body)
        self.assertIn("if (!state.agents.length) {", body)
        self.assertIn('later.forEach((b) => b.classList.add("secondary"))', body)
        self.assertIn("start.focus()", body)
        self.assertIn("issues/new?template=bug.yml", body)


class TypingTools(unittest.TestCase):
    def test_enter_waits_for_an_input_method_to_finish_its_word(self):
        """Round-2 re-review: answer boxes sent a half-typed Hindi word when the input method's Enter picked it."""
        for m in re.finditer(r'e\.key [!=]== "Enter"[^;{]*', PAGE):
            text = m.group(0)
            if "metaKey || e.ctrlKey" in text or 'e.key === " "' in text:
                continue  # Cmd/Ctrl+Enter, and Enter on a button, never come from an input method
            line = PAGE[PAGE.rfind("\n", 0, m.start()):PAGE.find("\n", m.end())]
            self.assertTrue("composing(e)" in line or "composing(e)" in PAGE[m.start() - 400:m.start()], line)
        self.assertIn("function composing(e) { return e.isComposing || e.keyCode === 229; }", PAGE)

    def test_the_chat_filter_finds_a_nukta_typed_either_way(self):
        self.assertIn('$("search").value.trim().normalize("NFC").toLowerCase()', PAGE)
        self.assertIn('.normalize("NFC").toLowerCase());', PAGE.split("function matches(node, want)")[1][:600])

    def test_mentions_of_you_show_in_any_case(self):
        self.assertIn('m[1].toLowerCase() === "owner" ? "Owner"', PAGE)

    def test_a_burst_is_one_announcement(self):
        self.assertIn('said.length + " new for you. Last: "', PAGE)


class Keys(unittest.TestCase):
    def test_windows_and_linux_are_shown_ctrl(self):
        self.assertIn('const MOD_K = MAC_KEYS ? "⌘K" : "Ctrl+K";', PAGE)
        self.assertIn('k.textContent = "Ctrl+K"', PAGE)  # one way of writing it, as in Settings
        self.assertNotIn('"Ctrl K"', PAGE)

    def test_ctrl_k_works_whatever_the_keyboard_layout(self):
        self.assertIn('(e.code === "KeyK" && !/^[a-z]$/i.test(e.key))', PAGE)
        self.assertIn('", " + MOD_K + " finds anything"', PAGE)

    def test_settings_sections_have_names_of_their_own(self):
        names = re.findall(r'^  [a-z]+: \["(?:app|project)", "([^"]+)"', PAGE, re.M)
        self.assertEqual(names.count("General"), 1)
        self.assertIn("Rules & skills", names)


class Phone(unittest.TestCase):
    def phone_css(self):
        return PAGE[PAGE.index("@media (max-width: 760px) {\n  body, body.panel-open"):]

    def test_one_alert_for_what_needs_you(self):
        css = self.phone_css()
        self.assertIn("#needs-open { display: none !important; }", css)
        self.assertIn("body:not(.needs-new) #needs-line { display: none !important; }", css)

    def test_each_tab_has_a_shape_of_its_own(self):
        css = self.phone_css()
        for icon in ("ti-chat", "ti-needs", "ti-tasks", "ti-agents"):
            self.assertIn("." + icon + " {", css)
        self.assertIn(".ti-needs::after", css)
        self.assertIn(".ti-tasks::after", css)

    def test_tasks_open_as_a_list_with_the_help_folded(self):
        self.assertIn('window.matchMedia("(max-width: 760px)").matches', PAGE)
        # Only the Tasks help folds: Needs you's "Nothing needs you now", the welcome and the commands' notes share
        # the class and must still show on a phone (audit, 14 Nov).
        self.assertIn("#tasks-help:not(.open) { display: none; }", self.phone_css())
        self.assertNotIn(".tasks-help:not(.open)", self.phone_css())
        self.assertIn('id="tasks-about"', PAGE)

    def test_the_task_forms_extra_fields_fold_again_after_adding(self):
        submit = PAGE[PAGE.index('$("task-form").addEventListener("submit"'):]
        submit = submit[:submit.index("\n});") + 4]
        self.assertIn('classList.toggle("has-text"', submit)


if __name__ == "__main__":
    unittest.main()
