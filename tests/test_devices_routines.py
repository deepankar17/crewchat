"""The phone and device proxy, and routines."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from crewchat_fixture import (  # noqa: E402,F401  (the shared server fixture)
    ProxyHarness,
    Base, Path, TMP, cli, connected, crewchat, eventually, io, json, mock, os, re, tempfile, threading, time, types,
    unittest, urllib,
)


class DeviceGate(Base):
    """D2: the server's gate for the mobile device proxy. The browser's rules, with apps for sites: mobile devices on
    per agent, one driver per device, real devices picked by the owner, allowed and trusted apps, and the owner's
    answers in Needs you. Emulators and simulators allow every app and change (owner, 5 Nov 2026)."""
    EMU, SIM, PHONE = "emulator-5554", "7C1D5A0E-3B6F-4F8A-9D2E-1A2B3C4D5E6F", "R58M123ABC"
    KINDS = {EMU: "emulator", SIM: "simulator", PHONE: "real"}

    def agent(self, mobile=True, folder="/work/app"):
        name = self.hub.agent_for(self.hub.open_session(self.place, "gemini"))
        key = crewchat.secrets.token_urlsafe(12)
        with self.hub.lock:
            self.hub.links[key] = name
            self.hub.agents[name]["session"] = {"folder": folder}
            self.hub.agents[name]["mobile"] = mobile
        self.hub.note_pids(self.place, key, [hash(name) % 90000 + 1000])
        proxy = self.hub.register_proxy(self.place, "gemini", folder, self.hub.hook_pids[name])
        self.addCleanup(self.forget, name)
        return name, proxy

    def forget(self, name):
        with self.hub.lock:
            self.hub.links = {k: n for k, n in self.hub.links.items() if n != name}
        crewchat.DEVICES.clear()

    def setUp(self):
        super().setUp()
        self.hub.roster.update("mobile_apps", lambda old: {})
        self.hub.roster.update("browser_sites", lambda old: {})
        with self.hub.lock:
            self.hub.device_app.clear()
            self.hub.device_ok.clear()
            self.hub.device_refused.clear()
        self.picked = [self.PHONE]
        patch = mock.patch.object(self.hub, "mobile_devices", side_effect=lambda: list(self.picked))
        patch.start()
        self.addCleanup(patch.stop)

    def call(self, proxy, tool, wait=2, **args):
        return self.hub.device_call(proxy, tool, args, kinds=self.KINDS, wait=wait)

    def seen(self, proxy, device, app):
        return self.hub.device_seen(proxy, [{"device": device, "app": app}])

    def answer_next(self, answer, asked=None):
        """Answer the next device ask in Needs you from another thread, as the owner on the page does."""
        def run():
            for _ in range(200):
                asks = [n for n in self.hub.needs_you() if n["kind"] == "device"]
                if asks:
                    if asked is not None:
                        asked.append(asks[0])
                    self.hub.answer_device(asks[0]["id"], answer)
                    return
                time.sleep(0.01)
        threading.Thread(target=run, daemon=True).start()

    def test_an_agent_without_mobile_devices_on_is_refused(self):
        _, proxy = self.agent(mobile=False)
        said = self.call(proxy, "mobile_take_screenshot", device=self.EMU)
        self.assertEqual(said["decision"], "deny")
        self.assertIn("mobile devices are not on", said["reason"])

    def test_on_an_emulator_or_simulator_nothing_asks(self):
        _, proxy = self.agent()
        for device in (self.EMU, self.SIM):
            self.assertEqual(self.call(proxy, "mobile_launch_app", device=device, packageName="com.bank.app")["decision"],
                             "allow")
            self.seen(proxy, device, "com.bank.app")
            self.assertEqual(self.call(proxy, "mobile_type_keys", device=device, text="x")["decision"], "allow")
        self.assertEqual([n for n in self.hub.needs_you() if n["kind"] == "device"], [])

    def test_taking_an_app_off_asks_on_any_device(self):
        _, proxy = self.agent()
        self.hub.set_mobile_app("com.example.shop", "trusted")
        asked = []
        self.answer_next("deny", asked)
        said = self.call(proxy, "mobile_uninstall_app", device=self.EMU, packageName="com.example.shop")
        self.assertEqual(said["decision"], "deny")
        self.assertIn("the owner denied this: do not uninstall com.example.shop", said["reason"])
        self.assertEqual((asked[0]["ask"], asked[0]["choices"]), ("manage", ["allow_task", "deny"]))
        self.answer_next("allow_task")
        self.assertEqual(self.call(proxy, "mobile_clear_app_data", device=self.SIM,
                                   packageName="com.example.shop")["decision"], "allow")
        self.assertEqual(self.call(proxy, "mobile_clear_app_data", device=self.SIM, packageName="com.example.shop",
                                   wait=0)["decision"], "deny")  # each time: an allow does not last

    def test_a_real_device_must_be_picked_by_the_owner(self):
        _, proxy = self.agent()
        self.picked = []
        said = self.call(proxy, "mobile_take_screenshot", device=self.PHONE, wait=0)
        self.assertEqual(said["decision"], "deny")
        self.assertIn("has not let agents use it", said["reason"])
        # A device Mobile MCP did not list is taken for a real one.
        said = self.hub.device_call(proxy, "mobile_take_screenshot", {"device": "emulator-9999"}, kinds={}, wait=0)
        self.assertIn("has not let agents use it", said["reason"])
        self.assertEqual(self.call(proxy, "mobile_list_available_devices")["decision"], "allow")  # no device: a read

    def test_a_cursor_turns_device_proxy_finds_its_agent_by_its_link_key(self):
        # 11 Nov e2e test: Cyd's every mobile call was refused (it cannot tell which agent), as each Cursor turn is a
        # new process and Cole and Cora had sessions in the same folder. Refused, Cyd used adb and simctl itself.
        for _ in range(2):
            self.agent(folder="/work/cyd")
        name, _ = self.agent(folder="/work/cyd")
        with self.hub.lock:
            key = next(k for k, n in self.hub.links.items() if n == name)
        lost = self.hub.register_proxy(self.place, "gemini", "/work/cyd", [99999])
        said = self.call(lost, "mobile_list_available_devices")
        self.assertEqual(said["decision"], "deny")
        self.assertIn(crewchat.DEVICE_WHO, said["reason"])
        self.assertIn(crewchat.DEVICE_NO_BYPASS, said["reason"])
        for word in ("adb", "xcrun simctl", "emulator"):
            self.assertIn(word, said["reason"])
        proxy = self.hub.register_proxy(self.place, "gemini", "/work/cyd", [99999], key)
        self.assertEqual(self.call(proxy, "mobile_list_available_devices")["decision"], "allow")
        self.assertEqual(self.call(proxy, "mobile_take_screenshot", device=self.SIM)["decision"], "allow")

    def test_a_proxy_under_a_cursor_turn_crewchat_started_is_that_agents(self):
        # 11 Nov retest of #114: Cursor did not hand its MCP servers the turn's CREWCHAT_LINK_KEY, so Cyd was still
        # refused. crewchat started the turn's process itself, so a proxy running under it is that agent's.
        for _ in range(2):
            self.agent(folder="/work/turn")
        name, _ = self.agent(folder="/work/turn")
        chat = "6f1c2a3b-0000-4000-8000-00000000c7d0"
        with self.hub.lock:
            self.hub.agents[name]["session"] = {"folder": "/work/turn", "id": chat}
        running = types.SimpleNamespace(poll=lambda: None)
        with mock.patch.dict(crewchat._TURNS, {chat: {"pid": 77001, "proc": running, "tool": "cursor"}}):
            proxy = self.hub.register_proxy(self.place, "gemini", "/work/turn", [77002, 77001, 400], "")
            self.assertEqual(self.hub.proxy_agent(proxy), name)
            self.assertEqual(self.call(proxy, "mobile_list_available_devices")["decision"], "allow")
            other = self.hub.register_proxy(self.place, "gemini", "/work/turn", [77003, 400], "")
            self.assertIsNone(self.hub.proxy_agent(other))
        done = types.SimpleNamespace(poll=lambda: 0)  # a turn that ended: its pid may be another process's now
        with mock.patch.dict(crewchat._TURNS, {chat: {"pid": 77001, "proc": done, "tool": "cursor"}}):
            self.assertIsNone(self.hub.proxy_agent(proxy))

    def test_every_device_refusal_says_never_to_go_around_crewchat(self):
        _, proxy = self.agent(mobile=False, folder="/work/off")
        said = self.call(proxy, "mobile_take_screenshot", device=self.EMU)
        self.assertEqual(said["decision"], "deny")
        self.assertIn("not on for", said["reason"])
        self.assertIn(crewchat.DEVICE_NO_BYPASS, said["reason"])
        self.assertEqual(said["reason"].count("Never go around crewchat"), 1)
        self.assertEqual(self.hub.device_call("feedfeedfeed", "mobile_take_screenshot", {}, wait=0),
                         {"decision": "deny", "reason": crewchat.PROXY_GONE})

    def test_a_real_device_not_picked_asks_the_owner_in_needs_you(self):
        # Owner, 11 Nov 2026: "Let agents use it" comes as a Needs you ask, not a refusal that sends them to Settings.
        _, proxy = self.agent()
        self.picked, asked, picks = [], [], []
        with mock.patch.object(self.hub, "pick_mobile_device", side_effect=lambda d, on: picks.append((d, on))):
            self.answer_next("allow_device", asked)
            said = self.call(proxy, "mobile_take_screenshot", device=self.PHONE)
        self.assertEqual(said["decision"], "allow")
        self.assertEqual((asked[0]["ask"], asked[0]["device"], asked[0]["choices"]),
                         ("device", self.PHONE, ["allow_device", "deny"]))
        self.assertIn("a real device", asked[0]["text"])
        self.assertEqual(picks, [(self.PHONE, True)])
        self.answer_next("deny")
        said = self.call(proxy, "mobile_take_screenshot", device="R58M-OTHER")
        self.assertEqual(said["decision"], "deny")
        self.assertIn("the owner denied this: do not use R58M-OTHER", said["reason"])

    def test_letting_agents_use_a_device_is_kept_in_this_machines_settings(self):
        self.hub.pick_mobile_device("R58M", True)
        self.assertIn("R58M", crewchat.load_config(self.hub.machine_root or self.hub.roster.root)["mobile_devices"])
        self.hub.pick_mobile_device("R58M", False)
        self.assertNotIn("R58M", crewchat.load_config(self.hub.machine_root or self.hub.roster.root)["mobile_devices"])

    def test_a_device_the_proxy_could_not_ask_about_is_refused_as_unknown(self):
        # The owner is asked, and told crewchat could not tell what it is; the Devices panel does not show it as a
        # phone.
        _, proxy = self.agent()
        self.picked, asked = [], []
        self.answer_next("deny", asked)
        said = self.hub.device_call(proxy, "mobile_take_screenshot", {"device": "Vibe_Samsung"},
                                    kinds={"Vibe_Samsung": "unknown"}, wait=2)
        self.assertEqual(said["decision"], "deny")
        self.assertIn("could not tell is an emulator", asked[0]["text"])
        said = self.hub.device_call(proxy, "mobile_take_screenshot", {"device": "Vibe_Samsung"},
                                    kinds={"Vibe_Samsung": "unknown"}, wait=0)
        self.assertIn("could not tell whether Vibe_Samsung is an emulator", said["reason"])
        self.assertIn("mobile_list_available_devices", said["reason"])
        self.assertNotIn("Vibe_Samsung", self.hub.device_kinds)

    def test_a_new_app_on_a_phone_asks_and_is_allowed_for_the_project(self):
        _, proxy = self.agent()
        asked = []
        self.answer_next("allow_app", asked)
        self.assertEqual(self.call(proxy, "mobile_launch_app", device=self.PHONE, packageName="com.example.shop")[
            "decision"], "allow")
        self.assertEqual((asked[0]["ask"], asked[0]["app"], asked[0]["device"], asked[0]["choices"]),
                         ("app", "com.example.shop", self.PHONE, ["allow_app", "deny"]))
        self.assertEqual(self.hub.mobile_apps(), {"com.example.shop": "allowed"})
        self.assertEqual(self.call(proxy, "mobile_launch_app", device=self.PHONE, packageName="com.example.shop",
                                   wait=0)["decision"], "allow")  # allowed now: no question

    def test_a_refused_app_is_refused_without_asking_again(self):
        _, proxy = self.agent()
        self.answer_next("deny")
        self.assertEqual(self.call(proxy, "mobile_launch_app", device=self.PHONE, packageName="com.bank.app")[
            "decision"], "deny")
        said = self.call(proxy, "mobile_launch_app", device=self.PHONE, packageName="com.bank.app", wait=0)
        self.assertIn("the owner denied this", said["reason"])

    def test_a_change_on_a_phone_asks_once_per_task_and_a_trusted_app_never(self):
        name, proxy = self.agent()
        self.hub.set_mobile_app("com.example.shop", "allowed")
        task = self.hub.add_row(crewchat.OWNER, "Check checkout on the phone")["id"]
        with mock.patch.object(self.hub, "_usage_task", return_value=task):
            self.seen(proxy, self.PHONE, "com.example.shop")
            self.assertEqual(self.call(proxy, "mobile_take_screenshot", device=self.PHONE, wait=0)["decision"], "allow")
            self.assertEqual(self.call(proxy, "mobile_type_keys", device=self.PHONE, text="x", wait=0)["decision"], "deny")
            self.answer_next("allow_task")
            self.assertEqual(self.call(proxy, "mobile_type_keys", device=self.PHONE, text="x")["decision"], "allow")
            self.assertEqual(self.call(proxy, "mobile_swipe_on_screen", device=self.PHONE, wait=0)["decision"], "allow")
        self.hub.set_mobile_app("com.other.app", "trusted")
        self.seen(proxy, self.PHONE, "com.other.app")
        self.assertEqual(self.call(proxy, "mobile_type_keys", device=self.PHONE, text="x", wait=0)["decision"], "allow")

    def test_always_allow_in_an_app_trusts_it(self):
        # Always allow on a change card trusts the app for the project: no agent is asked about changes there again.
        name, proxy = self.agent()
        self.hub.set_mobile_app("com.example.shop", "allowed")
        self.seen(proxy, self.PHONE, "com.example.shop")
        asked = []
        self.answer_next("trust_app", asked)
        self.assertEqual(self.call(proxy, "mobile_type_keys", device=self.PHONE, text="x")["decision"], "allow")
        self.assertEqual((asked[0]["ask"], asked[0]["choices"]), ("change", ["allow_task", "trust_app", "deny"]))
        self.assertEqual(self.hub.mobile_apps()["com.example.shop"], "trusted")
        self.assertEqual(self.call(proxy, "mobile_type_keys", device=self.PHONE, text="y", wait=0)["decision"], "allow")
        # An install is not a change in the app in front: it is never always allowed.
        done = []
        threading.Thread(target=lambda: done.append(self.call(proxy, "mobile_install_app", device=self.PHONE,
                                                              path="/tmp/a.apk", wait=5)), daemon=True).start()
        ask = eventually(lambda: next((n for n in self.hub.needs_you() if n["kind"] == "device"), None), what="the ask")
        self.assertEqual(ask["choices"], ["allow_task", "deny"])
        with self.assertRaises(crewchat.HubError):
            self.hub.answer_device(ask["id"], "trust_app")
        self.hub.answer_device(ask["id"], "deny")
        eventually(lambda: done, what="the call to end")

    def test_an_app_a_tap_brought_to_the_front_asks_before_the_next_call(self):
        _, proxy = self.agent()
        self.hub.set_mobile_app("com.example.shop", "trusted")
        self.seen(proxy, self.PHONE, "com.example.shop")
        note = self.seen(proxy, self.PHONE, "com.google.android.gm")
        self.assertIn("com.google.android.gm", note)
        said = self.call(proxy, "mobile_take_screenshot", device=self.PHONE, wait=0)  # a read too: it shows the mail
        self.assertEqual(said["decision"], "deny")
        self.assertEqual(self.seen(proxy, self.PHONE, "com.google.android.apps.nexuslauncher"), None)  # Home: fine
        self.assertEqual(self.call(proxy, "mobile_click_on_screen_at_coordinates", device=self.PHONE, x=1, y=1,
                                   wait=0)["decision"], "allow")
        self.assertIsNone(self.seen(proxy, self.EMU, "com.google.android.gm"))  # an emulator: nothing to say

    def test_a_batch_is_checked_step_by_step_on_the_app_each_step_leaves(self):
        _, proxy = self.agent()
        self.hub.set_mobile_app("com.example.shop", "trusted")
        steps = [{"name": "mobile_launch_app", "arguments": {"packageName": "com.example.shop"}},
                 {"name": "mobile_type_keys", "arguments": {"text": "x"}},
                 {"name": "mobile_launch_app", "arguments": {"packageName": "com.bank.app"}}]
        asked = []
        self.answer_next("deny", asked)
        said = self.call(proxy, "mobile_batch_commands", device=self.PHONE, steps=steps)
        self.assertEqual(said["decision"], "deny")
        self.assertEqual(asked[0]["app"], "com.bank.app")  # the typing in the trusted shop asked nothing

    def test_one_driver_per_device(self):
        _, first = self.agent(folder="/work/a")
        second_name, second = self.agent(folder="/work/b")
        self.assertEqual(self.call(first, "mobile_take_screenshot", device=self.EMU)["decision"], "allow")
        said = self.call(second, "mobile_take_screenshot", device=self.EMU)
        self.assertEqual(said["decision"], "deny")
        self.assertIn("is using %s" % self.EMU, said["reason"])
        self.assertEqual(self.call(second, "mobile_take_screenshot", device=self.SIM)["decision"], "allow")  # another
        self.hub.set_mobile(second_name, False)  # turned off: it lets its devices go
        self.assertNotIn(self.SIM, crewchat.DEVICES)

    def test_a_refused_call_does_not_take_the_device(self):
        _, proxy = self.agent()
        self.picked = []
        self.call(proxy, "mobile_take_screenshot", device=self.PHONE)
        self.answer_next("deny")
        self.call(proxy, "mobile_uninstall_app", device=self.EMU, packageName="a.b")
        self.assertEqual(crewchat.DEVICES, {})

    def test_a_web_address_on_a_phone_uses_the_browser_sites(self):
        _, proxy = self.agent()
        self.hub.set_browser_site("shop.example.com", "allowed")
        self.assertEqual(self.call(proxy, "mobile_open_url", device=self.PHONE, url="https://shop.example.com/x",
                                   wait=0)["decision"], "allow")
        asked = []
        self.answer_next("allow_site", asked)
        self.assertEqual(self.call(proxy, "mobile_open_url", device=self.PHONE, url="https://new.example.com/")[
            "decision"], "allow")
        self.assertEqual((asked[0]["ask"], asked[0]["choices"]), ("site", ["allow_site", "deny"]))
        self.assertIn("new.example.com", self.hub.browser_sites())

    def test_cloud_tools_and_unknown_tools(self):
        _, proxy = self.agent()
        self.assertIn("cloud devices are not", self.call(proxy, "mobile_allocate_remote_device")["reason"])
        self.seen(proxy, self.PHONE, "com.android.launcher3")
        # A tool by another name is Mobile MCP's, and a change: asked about on a real device.
        self.assertEqual(self.hub.device_call(proxy, "something_new", {"device": self.PHONE}, kinds=self.KINDS,
                                              wait=0)["decision"], "allow")  # on the home screen: nothing to ask
        self.hub.set_mobile_app("com.example.shop", "allowed")
        self.seen(proxy, self.PHONE, "com.example.shop")
        self.assertEqual(self.hub.device_call(proxy, "something_new", {"device": self.PHONE}, kinds=self.KINDS,
                                              wait=0)["decision"], "deny")

    def test_answers_must_fit_the_question(self):
        _, proxy = self.agent()
        done = []
        threading.Thread(target=lambda: done.append(self.call(proxy, "mobile_launch_app", device=self.PHONE,
                                                              packageName="com.x.y", wait=5)), daemon=True).start()
        ask = eventually(lambda: next((n for n in self.hub.needs_you() if n["kind"] == "device"), None), what="the ask")
        with self.assertRaises(crewchat.HubError):
            self.hub.answer_device(ask["id"], "allow_task")
        crewchat.post_json(self.base + "/api/needs", self.owner, {"key": ask["key"], "answer": "allow_app"})
        eventually(lambda: done, what="the call to go on")
        self.assertEqual(done[0]["decision"], "allow")

    def test_the_routes(self):
        name, proxy = self.agent()
        said = crewchat.post_json(self.base + "/api/hook", self.token,
                                  {"event": "device_call", "proxy": proxy, "tool": "mobile_take_screenshot",
                                   "args": {"device": self.EMU}, "kinds": {self.EMU: "emulator"}})
        self.assertEqual(said["decision"], "allow")
        seen = crewchat.post_json(self.base + "/api/hook", self.token,
                                  {"event": "device_seen", "proxy": proxy, "apps": [{"device": self.EMU, "app": "a.b"}]})
        self.assertEqual(seen, {"note": None})
        status = crewchat.post_json(self.base + "/api/devices", self.owner, {"op": "status"})
        self.assertEqual(status["holders"][self.EMU]["agent"], name)
        self.assertEqual(status["holders"][self.EMU]["app"], "a.b")
        self.assertEqual(status["kinds"][self.EMU], "emulator")
        crewchat.post_json(self.base + "/api/devices", self.owner, {"op": "app", "app": "com.x.y", "state": "trusted"})
        self.assertEqual(self.hub.mobile_apps(), {"com.x.y": "trusted"})
        crewchat.post_json(self.base + "/api/devices", self.owner, {"op": "release", "device": self.EMU})
        self.assertEqual(crewchat.DEVICES, {})
        crewchat.post_json(self.base + "/api/agent", self.owner, {"op": "mobile", "agent": name, "mode": "off"})
        self.assertFalse(self.hub.agents[name]["mobile"])
        with self.assertRaises(urllib.error.HTTPError):
            crewchat.post_json(self.base + "/api/devices", self.owner, {"op": "app", "app": "not a package", "state": "allowed"})

    def test_picking_a_real_device(self):
        self.addCleanup(self.hub.roster.update, "mobile_devices", lambda old: [])
        picked = lambda: crewchat.Hub.mobile_devices(self.hub)  # noqa: E731  (this machine's settings, not the stand-in)
        crewchat.post_json(self.base + "/api/devices", self.owner, {"op": "device", "device": self.PHONE, "on": True})
        self.assertEqual(picked(), [self.PHONE])
        crewchat.post_json(self.base + "/api/devices", self.owner, {"op": "device", "device": self.PHONE, "on": False})
        self.assertEqual(picked(), [])

    def test_the_page_answers_device_asks(self):
        page = crewchat.CHAT_PAGE
        self.assertIn('if (n.kind === "device") return deviceItem(n);', page)
        self.assertIn('["allow_app", "Allow the app"]', page)
        self.assertEqual(crewchat.NEED_KINDS["device"][3], "buttons")

    def test_a_task_moving_on_lets_its_devices_go(self):
        name, proxy = self.agent()
        task = self.hub.add_row(crewchat.OWNER, "Try the app")["id"]
        with mock.patch.object(self.hub, "_usage_task", return_value=task):
            self.call(proxy, "mobile_take_screenshot", device=self.EMU)
        self.assertEqual(crewchat.DEVICES[self.EMU]["task"], task)
        with self.hub.lock:
            self.hub._task_moved_on(task, "done")
        self.assertNotIn(self.EMU, crewchat.DEVICES)


    def test_a_mobile_task_is_taken_only_with_mobile_devices_on(self):
        # D4, as the browser's B3: a plan message, so every linked machine's sheet says so.
        task = self.hub.add_row(crewchat.OWNER, "Try the sign-up screen on Android")["id"]
        crewchat.post_json(self.base + "/api/task", self.owner, {"op": "mobile", "task": task, "value": True})
        plan = self.hub.messages[-1]
        self.assertEqual((plan["kind"], plan["task"], plan["mobile"]), ("plan", task, "on"))
        plain, _ = self.agent(mobile=False)
        with self.assertRaises(crewchat.HubError) as e:
            self.hub.take(plain, task)
        self.assertIn("needs a mobile device", str(e.exception))
        name, _ = self.agent()
        self.hub.take(name, task)
        self.assertTrue(next(r for r in self.hub.sheet_rows() if r["id"] == task)["mobile"])
        self.hub.set_task_mobile(task, False)
        self.assertFalse(self.hub.sheet[task]["mobile"])

    def test_the_agent_row_says_mobile_devices_are_on(self):
        name, _ = self.agent()
        self.assertTrue(next(r for r in self.hub.rows() if r["agent"] == name)["mobile"])
        plain, _ = self.agent(mobile=False)
        self.assertFalse(next(r for r in self.hub.rows() if r["agent"] == plain)["mobile"])

    def test_the_holder_is_reminded_to_post_progress(self):
        name, proxy = self.agent()
        key = next(k for k, n in self.hub.links.items() if n == name)
        task = self.hub.add_row(crewchat.OWNER, "Check the login screen")["id"]
        self.hub.take(name, task)
        now = [1000.0]
        with mock.patch.object(crewchat.time, "time", lambda: now[0]):
            self.assertEqual(self.call(proxy, "mobile_take_screenshot", device=self.EMU)["decision"], "allow")
            self.assertNotIn("remind", self.hub.hook(self.place, key, 0, 0, "tool"))
            now[0] += crewchat.BROWSER_NOTE_EVERY + 1
            self.call(proxy, "mobile_take_screenshot", device=self.EMU)  # still using it
            remind = self.hub.hook(self.place, key, 0, 0, "tool").get("remind", "")
            self.assertIn("mobile device", remind)
            self.assertIn("#%s" % task, remind)
            self.assertNotIn("remind", self.hub.hook(self.place, key, 0, 0, "tool"))  # once, not every call
            self.hub.update(name, task, "in_progress", "Signed up with a test account")  # a note: the clock starts again
            now[0] += crewchat.BROWSER_NOTE_EVERY - 10
            self.call(proxy, "mobile_take_screenshot", device=self.EMU)
            self.assertNotIn("remind", self.hub.hook(self.place, key, 0, 0, "tool"))

    def test_one_screenshot_is_no_reason_to_remind(self):
        # Two-machine test (11 Nov): a Cursor agent took one simulator screenshot, went on to other QA, and 5 minutes
        # later was told it had been "working on a mobile device for 5 minutes without a progress note". The reminder
        # goes by its device calls since the last note; the hold still lapses when the device has been idle.
        name, proxy = self.agent()
        key = next(k for k, n in self.hub.links.items() if n == name)
        task = self.hub.add_row(crewchat.OWNER, "Check the login screen")["id"]
        self.hub.take(name, task)
        now = [1000.0]
        with mock.patch.object(crewchat.time, "time", lambda: now[0]):
            self.assertEqual(self.call(proxy, "mobile_take_screenshot", device=self.SIM)["decision"], "allow")
            for _ in range(3):
                now[0] += crewchat.BROWSER_NOTE_EVERY / 2 + 1
                self.assertNotIn("remind", self.hub.hook(self.place, key, 0, 0, "tool"))
            now[0] = 1000.0 + crewchat.BROWSER_IDLE + 1
            status = crewchat.post_json(self.base + "/api/devices", self.owner, {"op": "status"})
            self.assertNotIn(self.SIM, status["holders"])  # let go once idle, as before
            self.assertEqual(self.call(proxy, "mobile_take_screenshot", device=self.SIM)["decision"], "allow")
            now[0] += crewchat.BROWSER_NOTE_EVERY + 1
            self.call(proxy, "mobile_take_screenshot", device=self.SIM)  # in use for 5 minutes now: that is work
            self.assertIn("mobile device", self.hub.hook(self.place, key, 0, 0, "tool").get("remind", ""))

    def test_the_panel_lists_the_devices_this_machine_sees(self):
        seen = [{"id": self.EMU, "platform": "android", "kind": "emulator", "name": "Pixel 9", "online": True}]
        with mock.patch.object(crewchat, "local_devices", return_value=seen):
            status = crewchat.post_json(self.base + "/api/devices", self.owner, {"op": "status"})
        self.assertEqual(status["devices"], seen)

    def test_local_devices_reads_adb_and_simctl(self):
        sims = {"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-18-0": [
            {"udid": self.SIM, "name": "iPhone 16", "state": "Booted"}]}}

        waits = []

        def status(command, env=None, timeout=15):
            waits.append(timeout)
            if command[0] == "/sdk/adb":
                return 0, ("List of devices attached\nemulator-5554 device product:sdk model:Pixel_9 device:emu\n"
                           "R58M123ABC unauthorized usb:1-1\n")
            return 0, json.dumps(sims)
        with mock.patch.object(crewchat, "find_adb", return_value="/sdk/adb"), \
                mock.patch.object(crewchat, "status_of", side_effect=status):
            found = crewchat.local_devices(mac=True)
        self.assertEqual([(d["id"], d["platform"], d["kind"], d["name"], d["online"]) for d in found],
                         [(self.EMU, "android", "emulator", "Pixel 9", True),
                          (self.PHONE, "android", "real", "", False),
                          (self.SIM, "ios", "simulator", "iPhone 16", True)])
        # An adb whose server cannot start takes 15 s to say so: the Devices panel waits a few seconds at most, on every
        # call (the device list, an emulator's AVD name, simctl).
        self.assertTrue(waits and all(w == crewchat.DEVICE_LIST_WAIT for w in waits), waits)
        self.assertLessEqual(crewchat.DEVICE_LIST_WAIT, 5)
        with mock.patch.object(crewchat, "find_adb", return_value=None):
            self.assertEqual(crewchat.local_devices(mac=False), [])

    def test_an_emulator_is_listed_by_its_avd_name_as_mobile_mcp_names_it(self):
        # 12 Nov Windows test: the panel said "emulator-5554 · sdk gphone16k x86 64" while the agent's tools named the
        # same emulator Pixel_10_Pro_XL, so once an agent used it the panel showed it twice ("Used by" on the second row).
        def status(command, env=None, timeout=15):
            if command[1:] == ["-s", "emulator-5554", "emu", "avd", "name"]:
                return 0, "Pixel_10_Pro_XL\r\nOK\r\n"
            return 0, ("List of devices attached\n"
                       "emulator-5554 device product:sdk_gphone16k_x86_64 model:sdk_gphone16k_x86_64 device:emu64xa16k\n"
                       "emulator-5556 offline\n")
        with mock.patch.object(crewchat, "find_adb", return_value="/sdk/adb"), \
                mock.patch.object(crewchat, "status_of", side_effect=status):
            found = crewchat.local_devices(mac=False)
        self.assertEqual([(d["id"], d["name"], d.get("serial")) for d in found],
                         [("Pixel_10_Pro_XL", "Pixel 10 Pro XL", "emulator-5554"),
                          ("emulator-5556", "", None)])  # offline: not asked, as before
        self.assertEqual(crewchat.avd_name("/sdk/adb", "emulator-5554"), "")  # no mock: nothing there to ask
        with mock.patch.object(crewchat, "status_of", return_value=(1, "error: device offline")):
            self.assertEqual(crewchat.avd_name("/sdk/adb", "emulator-5554"), "")

    def test_the_page_has_the_devices_panel_and_switches(self):
        page = crewchat.CHAT_PAGE
        self.assertIn('["devices", "Phones & emulators"]', page)  # a tab of Settings › Machines & devices (W16)
        self.assertIn("devices: panelDevices", page)
        self.assertIn('id="l-use-mobile"', page)
        self.assertIn('mobile: $("l-use-mobile").checked', page)
        self.assertIn('"&mobile="', page)
        self.assertIn('op: "mobile", agent: a.agent', page)
        self.assertIn('op: "mobile", task: r.id', page)
        ids = re.findall(r'\bid="([^"]+)"', page)
        self.assertEqual([i for i in set(ids) if ids.count(i) > 1], [])


class DeviceServers(unittest.TestCase):
    """D3: crewchat's "mobile" MCP server in a folder's settings for every tool, where an agent has devices on."""
    CLIENTS = (("claude", ".mcp.json", "mcpServers"), ("cursor", ".cursor/mcp.json", "mcpServers"),
               ("gemini", ".gemini/settings.json", "mcpServers"), ("qwen", ".qwen/settings.json", "mcpServers"),
               ("opencode", "opencode.json", "mcp"))

    def folder(self, client):
        folder = Path(tempfile.mkdtemp(prefix="mobile-%s-" % client, dir=TMP)).resolve()
        crewchat.install_client(client, folder, "http://127.0.0.1:9", "tok")
        return folder

    def test_every_tool_gets_the_mobile_server_and_loses_it(self):
        for client, rel, servers in self.CLIENTS:
            folder = self.folder(client)
            self.assertNotIn("mobile", json.loads((folder / rel).read_text())[servers], client)
            self.assertIn(rel, crewchat.sync_device_server(folder, True), client)
            entry = json.loads((folder / rel).read_text())[servers]["mobile"]
            self.assertIn('"device-proxy", "%s"' % client, json.dumps(entry), client)
            self.assertTrue(crewchat.is_device(client, entry), client)
            self.assertIn(crewchat.SERVER_NAME, json.loads((folder / rel).read_text())[servers])  # crewchat's stays
            if client != "cursor":  # the owner's Allow can take PERMISSION_WAIT inside a call
                self.assertGreaterEqual(entry["timeout"], crewchat.PERMISSION_WAIT * 1000, client)
            self.assertEqual(crewchat.sync_device_server(folder, True), [], client)  # up to date
            crewchat.sync_device_server(folder, False)
            self.assertNotIn("mobile", json.loads((folder / rel).read_text())[servers], client)
        home = Path(tempfile.mkdtemp(prefix="codex-mobile-", dir=TMP))
        for used in (False, True):
            with mock.patch.object(crewchat, "codex_config", return_value=home / "config.toml"), \
                    mock.patch.object(crewchat, "devices_used", return_value=used):
                crewchat.install_codex()
            text = (home / "config.toml").read_text()
            self.assertEqual("[mcp_servers.mobile]" in text, used)
        self.assertIn('"device-proxy", "codex"', text)
        self.assertIn("CREWCHAT_DEVICE", text)

    def test_claude_code_turns_the_server_on_and_allows_its_tools(self):
        folder = self.folder("claude")
        local = folder / ".claude" / "settings.local.json"
        crewchat.sync_device_server(folder, True)
        settings = json.loads(local.read_text())
        self.assertIn("mobile", settings["enabledMcpjsonServers"])  # else Claude Code asks about a new .mcp.json server
        self.assertIn("mcp__mobile", settings["permissions"]["allow"])  # the device gate asks what needs asking
        crewchat.sync_device_server(folder, False)
        settings = json.loads(local.read_text())
        self.assertNotIn("mobile", settings.get("enabledMcpjsonServers", []))
        self.assertNotIn("mcp__mobile", settings.get("permissions", {}).get("allow", []))

    def test_cursor_allows_the_mobile_servers_tools(self):
        # Without it, a Cursor agent in the background (cursor-agent -p) cannot use the devices it was given: the device
        # gate asks what needs asking, as for Claude Code.
        folder = self.folder("cursor")
        cli = folder / ".cursor" / "cli.json"
        self.assertIn(".cursor/cli.json", crewchat.sync_device_server(folder, True))
        self.assertIn("Mcp(mobile:*)", json.loads(cli.read_text())["permissions"]["allow"])
        crewchat.sync_device_server(folder, False)
        self.assertNotIn("Mcp(mobile:*)", cli.read_text())

    def test_a_mobile_server_of_the_owners_own_is_left_alone(self):
        theirs = {"command": "npx", "args": ["-y", "@mobilenext/mobile-mcp@latest"]}
        for client, rel, servers in self.CLIENTS:
            folder = self.folder(client)
            data = json.loads((folder / rel).read_text())
            data[servers]["mobile"] = theirs
            (folder / rel).write_text(json.dumps(data))
            self.assertFalse(crewchat.is_device(client, theirs))
            for on in (True, False):
                self.assertNotIn(rel, crewchat.sync_device_server(folder, on), client)
                self.assertEqual(json.loads((folder / rel).read_text())[servers]["mobile"], theirs, client)
        home = Path(tempfile.mkdtemp(prefix="codex-mobile-theirs-", dir=TMP))
        (home / "config.toml").write_text('[mcp_servers.mobile]\ncommand = "npx"\n')
        with mock.patch.object(crewchat, "codex_config", return_value=home / "config.toml"), \
                mock.patch.object(crewchat, "devices_used", return_value=True):
            crewchat.install_codex()
        text = (home / "config.toml").read_text()
        self.assertEqual(text.count("[mcp_servers.mobile]"), 1)
        self.assertNotIn("device-proxy", text)

    def test_a_settings_file_git_tracks_is_left_alone(self):
        folder = self.folder("gemini")
        with mock.patch.object(crewchat, "tracked_by_git", return_value=True):
            self.assertEqual(crewchat.sync_device_server(folder, True), [])
        self.assertNotIn("mobile", json.loads((folder / ".gemini/settings.json").read_text())["mcpServers"])

    def test_the_guide_has_the_mobile_rules(self):
        self.assertIn("## Mobile devices", crewchat.MOBILE_GUIDE)
        for rule in ("One agent uses a device at a time", "Never type a password", "screenshot",
                     "Uninstalling an app or clearing its\n  data always asks"):
            self.assertIn(rule, crewchat.MOBILE_GUIDE)
        # 11 Nov e2e test: an agent the gate refused listed devices with adb and simctl and took a screenshot itself.
        for rule in ("Use devices only through the mobile tools", "adb", "xcrun simctl", "emulator",
                     "even when\n  crewchat refuses a call"):
            self.assertIn(rule, crewchat.MOBILE_GUIDE)
        for rule in ("Use the owner's Chrome only through the browser tools", "AppleScript", "debugging port"):
            self.assertIn(rule, crewchat.BROWSER_GUIDE)
        with mock.patch.object(crewchat, "load_config", return_value={"project": "Demo"}), \
                mock.patch.object(crewchat, "project_rules", return_value=""), \
                mock.patch.object(crewchat, "project_skills", return_value=[]):
            self.assertIn(crewchat.MOBILE_GUIDE, crewchat.project_guide(TMP))

    def test_the_guide_points_to_the_skill_libraries(self):
        with mock.patch.object(crewchat, "load_config", return_value={"project": "Demo"}), \
                mock.patch.object(crewchat, "project_rules", return_value=""), \
                mock.patch.object(crewchat, "project_skills", return_value=[]):
            guide = crewchat.project_guide(TMP)
        self.assertIn("## Skill libraries", guide)
        for url in ("https://github.com/anthropics/skills", "https://github.com/vercel-labs/agent-skills",
                    "https://github.com/openai/skills", "https://github.com/google/skills",
                    "https://github.com/huggingface/skills", "https://github.com/mattpocock/skills",
                    "https://github.com/obra/superpowers", "https://github.com/addyosmani/agent-skills",
                    "https://github.com/VoltAgent/awesome-agent-skills"):
            self.assertIn(url, guide)
        for rule in ("When you plan a task", "licence", "ask the owner", "`crewchat skills add`",
                     "never give a skill a secret"):
            self.assertIn(rule, crewchat.SKILL_LIBRARIES_GUIDE)

    def test_server_start_keeps_the_mobile_server_up_to_date(self):
        folder = Path(tempfile.mkdtemp(prefix="mobile-start-", dir=TMP)).resolve()
        hub = mock.Mock()
        hub.project_folders.return_value = [folder]
        hub.live_processes.return_value = []
        project = types.SimpleNamespace(hub=hub, roster=types.SimpleNamespace(root=folder), name="demo")
        projects = types.SimpleNamespace(all=lambda: [project])
        with mock.patch.object(crewchat, "refresh_codex"), mock.patch.object(crewchat, "write_guides"), \
                mock.patch.object(crewchat, "update_hooks"):
            crewchat.upgrade_folders(projects)
        hub.sync_mobile.assert_called_once_with([folder])


class DeviceReadiness(unittest.TestCase):
    """D3: what agents need to use mobile devices here: npx, and an Android device online or a booted Simulator."""

    def ready(self, npx=True, adb="/sdk/platform-tools/adb", adb_out="", simctl=(0, ""), mac=True):
        self.waits = []

        def status(command, env=None, timeout=15):
            self.waits.append(timeout)
            return (0, adb_out) if command[0] == adb else simctl
        with mock.patch.object(crewchat.shutil, "which", side_effect=lambda n: "/usr/bin/npx" if npx and n == "npx" else None), \
                mock.patch.object(crewchat, "find_adb", return_value=adb), \
                mock.patch.object(crewchat, "status_of", side_effect=status):
            return crewchat.device_readiness(mac=mac)

    def states(self, items):
        return [(i["state"], i["text"]) for i in items]

    def test_an_online_emulator_is_ready(self):
        items = self.ready(adb_out="List of devices attached\nemulator-5554\tdevice\n\n")
        self.assertEqual([i["state"] for i in items if i["state"] == "todo"], [])
        self.assertIn(("ok", "Android: emulator-5554 online"), self.states(items))
        self.assertIn("1.0.16", items[0]["text"])  # the pinned Mobile MCP

    def test_an_online_emulator_is_named_by_its_avd_name(self):
        def status(command, env=None, timeout=15):
            if command[-3:] == ["emu", "avd", "name"]:
                return 0, "Pixel_10_Pro_XL\nOK\n"
            return 0, "List of devices attached\nemulator-5554\tdevice\n\n"
        with mock.patch.object(crewchat.shutil, "which", return_value="/usr/bin/npx"), \
                mock.patch.object(crewchat, "find_adb", return_value="/sdk/adb"), \
                mock.patch.object(crewchat, "status_of", side_effect=status):
            items = crewchat.device_readiness(mac=False)
        self.assertIn(("ok", "Android: Pixel_10_Pro_XL (emulator-5554) online"), self.states(items))

    def test_a_stalled_adb_or_simctl_does_not_hold_up_the_readiness_list(self):
        # The agent's settings ask for this list: an adb whose server cannot start takes 15 s to say so, which a page
        # (and the e2e test that waits for it) must not wait out, as the Devices panel does not.
        self.ready(adb_out="List of devices attached\n\n", simctl=(0, "== Devices ==\n"))
        self.assertEqual(self.waits, [crewchat.DEVICE_LIST_WAIT] * 2)

    def test_a_booted_simulator_is_ready_on_a_mac(self):
        items = self.ready(adb=None, simctl=(0, "== Devices ==\n-- iOS 18.0 --\n    iPhone 16 (7C1D) (Booted)\n"))
        self.assertIn(("ok", "iOS: a Simulator is booted"), self.states(items))
        self.assertEqual([i for i in items if i["state"] == "todo"], [])

    def test_nothing_online_says_what_to_start(self):
        items = self.ready(adb_out="List of devices attached\n\n", simctl=(0, "== Devices ==\n"))
        self.assertTrue(any(i["state"] == "todo" and "Start an Android emulator" in i["text"] for i in items))

    def test_a_phone_waiting_for_usb_debugging_is_a_todo(self):
        items = self.ready(adb_out="List of devices attached\nR58M123ABC\tunauthorized\n")
        self.assertIn(("todo", "Android: R58M123ABC is waiting for you to allow USB debugging on the phone"),
                      self.states(items))

    def test_no_npx_and_no_mac(self):
        items = self.ready(npx=False, adb_out="List of devices attached\nemulator-5554\tdevice\n", mac=False)
        self.assertEqual(items[0]["state"], "todo")
        self.assertIn("iOS Simulators need a Mac", [i["text"] for i in items])

    def test_adb_is_found_where_android_studio_puts_it(self):
        sdk = Path(tempfile.mkdtemp(prefix="sdk-", dir=TMP))
        (sdk / "platform-tools").mkdir()
        (sdk / "platform-tools" / "adb").write_text("")
        with mock.patch.object(crewchat.shutil, "which", return_value=None), \
                mock.patch.dict(os.environ, {"ANDROID_HOME": str(sdk)}):
            self.assertEqual(crewchat.find_adb(), str(sdk / "platform-tools" / "adb"))
        self.assertEqual(crewchat.adb_devices("List of devices attached\n* daemon started\nX\tdevice\n"),
                         {"X": "device"})

    def test_adb_is_found_in_android_studios_windows_and_linux_folders(self):
        # Windows run: the Devices panel said no device was online while `adb devices` listed the running emulator.
        # Android Studio put adb in %LOCALAPPDATA%\Android\Sdk, set no ANDROID_HOME, and left it off PATH.
        local = Path(tempfile.mkdtemp(prefix="localappdata-", dir=TMP))
        (local / "Android" / "Sdk" / "platform-tools").mkdir(parents=True)
        (local / "Android" / "Sdk" / "platform-tools" / "adb.exe").write_text("")
        user = Path(tempfile.mkdtemp(prefix="linux-home-", dir=TMP))
        (user / "Android" / "Sdk" / "platform-tools").mkdir(parents=True)
        (user / "Android" / "Sdk" / "platform-tools" / "adb").write_text("")
        env = {k: v for k, v in os.environ.items() if k not in ("ANDROID_HOME", "ANDROID_SDK_ROOT", "LOCALAPPDATA")}
        empty = Path(tempfile.mkdtemp(prefix="no-sdk-", dir=TMP))
        with mock.patch.object(crewchat.shutil, "which", return_value=None):
            with mock.patch.dict(os.environ, dict(env, LOCALAPPDATA=str(local), HOME=str(empty), USERPROFILE=str(empty)),
                                 clear=True):
                self.assertEqual(crewchat.find_adb(), str(local / "Android" / "Sdk" / "platform-tools" / "adb.exe"))
            with mock.patch.dict(os.environ, dict(env, HOME=str(user), USERPROFILE=str(user)), clear=True):
                self.assertEqual(crewchat.find_adb(), str(user / "Android" / "Sdk" / "platform-tools" / "adb"))

    def test_tool_readiness_adds_the_device_lines(self):
        with mock.patch.object(crewchat, "find_tool", return_value="/usr/bin/gemini"), \
                mock.patch.object(crewchat, "signed_in", return_value=crewchat.ready_item("ok", "signed in")), \
                mock.patch.object(crewchat, "device_readiness", return_value=[crewchat.ready_item("ok", "dev")]):
            self.assertEqual(crewchat.tool_readiness("gemini", fresh=True, mobile=True)[-1]["text"], "dev")
            self.assertNotIn("dev", [i["text"] for i in crewchat.tool_readiness("gemini", fresh=True)])


class DeviceProxy(ProxyHarness, unittest.TestCase):
    """D1: crewchat's mobile bridge (Android and iOS) runs behind `crewchat device-proxy`, which gates each call, refuses Mobile MCP's
    cloud devices, and reads the app in front from the device (a fake Mobile MCP child here). What the proxy does
    whatever its child (a dead child, a cancelled call, notifications) is GatedProxy's, tested on BrowserProxy."""
    FAKE = str(Path(__file__).resolve().parent / "fake_mobile_mcp.py")
    PROXY = "DeviceProxy"
    NAME = "fake-mobile"
    EMU = "emulator-5554"

    def tap(self, **args):
        return self.result("mobile_click_on_screen_at_coordinates", device=self.EMU, x=10, y=10, **args)

    def test_calls_pass_through_when_allowed(self):
        self.start()
        result = self.result("mobile_launch_app", device=self.EMU, packageName="com.example.shop")
        self.assertIn("com.example.shop", result["content"][0]["text"])
        self.assertEqual(self.server.calls[0][:2], ("mobile_launch_app",
                                                    {"device": self.EMU, "packageName": "com.example.shop"}))
        self.assertEqual(self.server.seen_tabs, [[{"device": self.EMU, "app": "com.example.shop"}]])

    def test_a_saved_screenshot_goes_where_it_can_be_shared_as_it_is(self):
        # 11 Nov two-machine test: an agent saved an iOS simulator screenshot in the temporary folder (Mobile MCP saves
        # only there or in the project), moved it into the project with Bash and opened it with Read: two more
        # questions. Saved outside the project, it goes to crewchat's private screenshot folder, which hub_send and
        # hub_task share from as it is, alone or as a step of a batch; Mobile MCP says where.
        self.start()
        asked = str(Path(tempfile.mkdtemp(dir=TMP)) / "iphone17-sim.png")
        text = self.result("mobile_save_screenshot", device=self.EMU, saveTo=asked)["content"][0]["text"]
        saved = Path(text.split("Screenshot saved to: ", 1)[1].strip())
        self.assertTrue(saved.parent.name.startswith(crewchat.CREWCHAT_SHOTS), text)
        self.assertEqual(saved.name, "iphone17-sim.png")
        self.assertFalse(Path(asked).exists())
        self.assertTrue(crewchat.saved_screenshot(saved.resolve()))
        if hasattr(os, "getuid"):
            self.assertEqual(saved.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.server.calls[-1][1]["saveTo"], asked)  # the gate saw the agent's own call
        steps = [{"name": "mobile_save_screenshot", "arguments": {"saveTo": "/elsewhere/../x/after tap.png"}},
                 {"name": "mobile_launch_app", "arguments": {"packageName": "com.example.shop"}}]
        text = self.result("mobile_batch_commands", device=self.EMU, steps=steps)["content"][0]["text"]
        self.assertIn(str(saved.parent / "after_tap.png"), text)
        self.assertTrue((saved.parent / "after_tap.png").is_file())

    def test_a_screenshot_saved_in_the_project_stays_there(self):
        project = Path(tempfile.mkdtemp(dir=TMP)).resolve()
        made = []
        folder = lambda: made.append(1) or "/shots"  # noqa: E731
        for asked in ("shots/a.png", str(project / "b.png"), str(project / "x" / ".." / "c.png")):
            self.assertEqual(crewchat.screenshot_path(asked, project, folder), asked)
        self.assertEqual(made, [])  # no folder made for nothing
        for asked, name in (("/tmp/a.png", "a.png"), (str(project / ".." / "b.png"), "b.png"),
                            ("../c.png", "c.png"), ("~/Desktop/d.png", "d.png"), ("/tmp/.e.png", "e.png")):
            self.assertEqual(crewchat.screenshot_path(asked, project, folder), str(Path("/shots") / name), asked)
        self.assertEqual(crewchat.screenshot_path("", project, folder), "")  # Mobile MCP says what is wrong
        self.assertIn("mobile_save_screenshot", crewchat.MOBILE_GUIDE)

    def test_a_refused_call_is_not_forwarded(self):
        self.start()
        self.server.deny = "the owner denied this: do not open com.bank.app"
        result = self.result("mobile_launch_app", device=self.EMU, packageName="com.bank.app")
        self.assertTrue(result["isError"])
        self.assertIn("the owner denied this", result["content"][0]["text"])
        self.assertEqual(self.child_calls(), ["mobile_list_available_devices"])  # the proxy's own question only
        self.assertEqual(self.server.seen_tabs, [])

    def test_each_device_is_said_to_be_what_mobile_mcp_lists_it_as(self):
        # D2: an emulator or a simulator asks nothing; the proxy reads which a device is from Mobile MCP's own list,
        # once, and a device the list does not have is a real one.
        self.start(env={"FAKE_DEVICES": json.dumps([{"id": self.EMU, "type": "emulator"},
                                                    {"id": "SIM-1", "type": "simulator"},
                                                    {"id": "R58M", "type": "real"}, {"id": "odd", "type": "toaster"}])})
        for device in (self.EMU, "SIM-1", "R58M", "odd", "gone", self.EMU):
            self.result("mobile_take_screenshot", device=device)
        self.assertEqual([a["kinds"] for a in self.server.about],
                         [{self.EMU: "emulator"}, {"SIM-1": "simulator"}, {"R58M": "real"}, {"odd": "real"},
                          {"gone": "real"}, {self.EMU: "emulator"}])
        self.assertEqual(self.child_calls().count("mobile_list_available_devices"), 2)  # again only for "gone"

    def test_the_agents_own_device_list_says_what_each_device_is(self):
        # Live test of dev 953d796: an Android emulator (Mobile MCP's id is its AVD name) and an iOS simulator were
        # refused as real devices when the proxy's own list did not answer. The list the agent asked for says it too.
        self.start(env={"FAKE_LIST_ONCE": "1", "FAKE_DEVICES": json.dumps([
            {"id": "Vibe_Samsung", "platform": "android", "type": "emulator", "state": "online"},
            {"id": "1917AD71-AAAA", "platform": "ios", "type": "simulator", "state": "online"}])})
        self.assertIn("Vibe_Samsung", self.result("mobile_list_available_devices")["content"][0]["text"])
        for device in ("Vibe_Samsung", "1917AD71-AAAA"):
            self.result("mobile_take_screenshot", device=device)
        self.assertEqual([a.get("kinds") for a in self.server.about[1:]],
                         [{"Vibe_Samsung": "emulator"}, {"1917AD71-AAAA": "simulator"}])
        self.assertEqual(self.child_calls().count("mobile_list_available_devices"), 1)  # the agent's: no need to ask

    def test_a_device_list_that_does_not_answer_leaves_the_device_unknown(self):
        # Not "real": the owner's agent is told crewchat could not tell, not that an emulator is a phone.
        self.start(env={"FAKE_LIST_ONCE": "1"})
        self.result("mobile_list_available_devices")
        self.result("mobile_take_screenshot", device="Pixel_9")  # not in the first list; the second fails
        self.assertEqual(self.server.about[-1]["kinds"], {"Pixel_9": "unknown"})

    def test_adbs_name_for_an_emulator_is_an_emulator_as_the_devices_panel_says(self):
        # Windows run: Mobile MCP listed the emulator by its AVD name, the agent used adb's emulator-5554 (as the Devices
        # panel shows it, an emulator), and the owner was asked about "emulator-5554, a real device".
        self.start(env={"FAKE_DEVICES": json.dumps([{"id": "Pixel_9", "platform": "android", "type": "emulator"},
                                                    {"id": "R58M", "type": "real"}])})
        for device in ("emulator-5554", "R58M", "emulator-5554x"):
            self.result("mobile_take_screenshot", device=device)
        self.assertEqual([a["kinds"] for a in self.server.about],
                         [{"emulator-5554": "emulator"}, {"R58M": "real"}, {"emulator-5554x": "real"}])
    def test_a_tap_that_brings_another_app_to_the_front_is_seen(self):
        self.start()
        self.result("mobile_launch_app", device=self.EMU, packageName="com.example.shop")
        self.tap(goes_to="com.android.chrome")
        self.assertEqual(self.server.seen_tabs[-1], [{"device": self.EMU, "app": "com.android.chrome"}])

    def test_the_app_comes_from_the_device_not_from_its_name(self):
        # An app's name is its own text: one that calls itself "Shop (com.example.shop)" is still the app it is.
        self.start(env={"FAKE_NAME": "Shop (com.example.shop), activity: com.example.shop.Main"})
        self.result("mobile_launch_app", device=self.EMU, packageName="com.evil.app")
        self.assertEqual(self.server.seen_tabs[-1], [{"device": self.EMU, "app": "com.evil.app"}])

    def test_cloud_devices_are_refused_by_the_proxy(self):
        self.start()
        for tool, args in (("mobile_allocate_remote_device", {}), ("mobile_login_to_cloud_provider", {}),
                           ("mobile_batch_commands", {"device": self.EMU, "steps": [
                               {"name": "mobile_take_screenshot", "arguments": {}},
                               {"name": "mobile_list_remote_devices", "arguments": {}}]})):
            result = self.result(tool, **args)
            self.assertTrue(result["isError"])
            self.assertIn("cloud devices are not used", result["content"][0]["text"])
        self.assertEqual((self.server.calls, self.child_calls()), ([], []))

    def test_a_batch_is_asked_about_as_a_whole_and_every_device_it_used_is_seen(self):
        self.start()
        steps = [{"name": "mobile_launch_app", "arguments": {"packageName": "com.example.shop"}},
                 {"name": "mobile_launch_app", "arguments": {"device": "emulator-5556", "packageName": "com.mail"}}]
        self.result("mobile_batch_commands", device=self.EMU, steps=steps)
        self.assertEqual(self.server.calls[0][0], "mobile_batch_commands")
        self.assertEqual(self.server.seen_tabs[-1], [{"device": self.EMU, "app": "com.example.shop"},
                                                     {"device": "emulator-5556", "app": "com.mail"}])

    def test_a_call_on_no_device_tells_the_server_nothing(self):
        self.start()
        self.assertIn("emulator-5554", self.result("mobile_list_available_devices")["content"][0]["text"])
        self.assertEqual(self.server.seen_tabs, [])

    def test_an_ios_simulator_is_driven_the_same_way(self):
        # Mobile MCP drives the iOS Simulator with the same tools: a simulator's id is a UUID, an app a bundle id.
        self.start()
        sim = "7C1D5A0E-3B6F-4F8A-9D2E-1A2B3C4D5E6F"
        self.result("mobile_launch_app", device=sim, packageName="com.example.my-app")
        self.result("mobile_click_on_screen_at_coordinates", device=sim, x=5, y=5, goes_to="com.apple.mobilesafari")
        self.assertEqual(self.server.seen_tabs, [[{"device": sim, "app": "com.example.my-app"}],
                                                 [{"device": sim, "app": "com.apple.mobilesafari"}]])

    def test_an_app_the_device_cannot_name_is_none(self):
        self.start(env={"FAKE_FRONT": "none"})
        self.tap()
        self.assertEqual(self.server.seen_tabs, [[{"device": self.EMU, "app": None}]])

    def test_server_down_refuses_changes_allows_reads(self):
        self.start()
        self.server.down = True
        result = self.tap()
        self.assertTrue(result["isError"])
        self.assertIn(crewchat.BROWSER_DOWN, result["content"][0]["text"])
        result = self.result("mobile_take_screenshot", device=self.EMU, text="the screen")
        self.assertEqual(result["content"][0]["text"], "the screen")
        self.assertNotIn("mobile_click_on_screen_at_coordinates", self.child_calls())
        self.assertIn("mobile_take_screenshot", self.child_calls())

    def test_calls_are_gated_one_at_a_time(self):
        self.start()
        self.call("mobile_launch_app", device=self.EMU, packageName="com.example.shop")
        self.call("mobile_take_screenshot", device=self.EMU, text="x")
        self.assertEqual({self.read()["id"], self.read()["id"]}, {1, 2})
        self.assertEqual([(c[0], c[2]) for c in self.server.calls],
                         [("mobile_launch_app", 0), ("mobile_take_screenshot", 1)])

    def test_mobile_mcp_runs_without_telemetry_or_other_url_schemes(self):
        dump = Path(tempfile.mkdtemp(prefix="proxy-env-", dir=TMP)) / "env"
        self.start(env={"FAKE_ENV": str(dump), "MOBILEMCP_ALLOW_UNSAFE_URLS": "1", "MOBILEMCP_LEGACY_ROBOT": "1"})
        self.assertEqual(json.loads(dump.read_text()), {"MOBILEMCP_DISABLE_TELEMETRY": "1",
                                                        "MOBILEMCP_ALLOW_UNSAFE_URLS": None,
                                                        "MOBILEMCP_LEGACY_ROBOT": None})

    def test_a_device_that_cannot_start_answers_with_an_error(self):
        self.start(child=["/nonexistent/npx-%s" % os.getpid()])
        result = self.result("mobile_take_screenshot", device=self.EMU)
        self.assertTrue(result["isError"])
        self.assertIn("the mobile device could not start", result["content"][0]["text"])

    def test_an_unexpected_error_still_answers_the_call(self):
        self.start()
        self.server.broken = True
        result = self.result("mobile_take_screenshot", device=self.EMU)
        self.assertTrue(result["isError"])
        self.assertIn("crewchat's mobile bridge failed", result["content"][0]["text"])
        self.assertNotIn("mobile_take_screenshot", self.child_calls())

    def test_a_batch_is_refused_not_passed_on(self):
        self.start()
        self.send([{"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                    "params": {"name": "mobile_launch_app", "arguments": {"device": self.EMU, "packageName": "x.y"}}}])
        answer = self.read()
        self.assertEqual(answer["error"]["code"], -32600)
        self.assertIn("crewchat's mobile bridge", answer["error"]["message"])
        self.assertEqual((self.child_calls(), self.server.calls), ([], []))

    def test_the_real_proxy_finds_npx_and_reads_utf8(self):
        made = {}

        class Fake:
            def __init__(self, client, child, server, stdin, stdout, env=None):
                made.update(child=child, server=server, stdin=stdin, stdout=stdout)

            def run(self):
                return 0
        # Its own stand-ins for stdin and stdout: a wrapper around the real ones closes them when it goes.
        pipes = types.SimpleNamespace(buffer=io.BytesIO())
        with mock.patch.object(crewchat, "DeviceProxy", Fake), mock.patch.object(crewchat, "find_project", return_value=None), \
                mock.patch.object(crewchat.shutil, "which", return_value="C:/node/npx.cmd"), \
                mock.patch.object(crewchat.sys, "stdin", pipes), mock.patch.object(crewchat.sys, "stdout", pipes):
            self.assertEqual(crewchat.device_proxy_main("gemini"), 0)
        self.assertEqual(made["child"], ["C:/node/npx.cmd", "-y", crewchat.MOBILE_MCP])
        self.assertRegex(crewchat.MOBILE_MCP, r"^@mobilenext/mobile-mcp@\d+\.\d+\.\d+$")  # pinned, never @latest
        self.assertEqual(made["server"].call("mobile_take_screenshot", {}),
                         {"decision": "deny", "reason": crewchat.DEVICE_ONLY})  # no chat here: only the chat's agents
        self.assertEqual((made["stdin"].encoding.lower(), made["stdout"].encoding.lower()), ("utf-8", "utf-8"))


class DeviceCalls(unittest.TestCase):
    """D1: what a Mobile MCP call does, as the device gate (D2) and a proxy without a server sort it."""

    def test_tools_by_any_name_their_tool_gives_them(self):
        for tool in ("mobile_launch_app", "mcp__mobile__mobile_launch_app", "mobile_mobile_launch_app"):
            self.assertEqual(crewchat.device_tool_name(tool), "launch_app", tool)
        for tool in ("browser_click", "mcp__mobile__launch_app", "mobilelaunch", ""):
            self.assertEqual(crewchat.device_tool_name(tool), "", tool)

    def test_each_kind_of_call(self):
        cases = {("mobile_take_screenshot", ()): ("read", ""),
                 ("mobile_list_elements_on_screen", ()): ("read", ""),
                 ("mobile_click_on_screen_at_coordinates", ()): ("change", ""),
                 ("mobile_type_keys", ()): ("change", ""),
                 ("mobile_something_new", ()): ("change", ""),
                 ("mobile_launch_app", (("packageName", "com.a.b"),)): ("launch", "com.a.b"),
                 ("mobile_open_url", (("url", "https://x.example/"),)): ("navigate", "https://x.example/"),
                 ("mobile_install_app", (("path", "/w/app.apk"),)): ("install", "/w/app.apk"),
                 ("mobile_uninstall_app", (("packageName", "com.a.b"),)): ("manage", "com.a.b"),
                 ("mobile_clear_app_data", (("packageName", "com.a.b"),)): ("manage", "com.a.b"),
                 ("mobile_allocate_remote_device", ()): ("cloud", ""),
                 ("browser_click", ()): ("other", "")}
        for (tool, args), want in cases.items():
            self.assertEqual(crewchat.device_action(tool, dict(args)), want, tool)

    def test_a_batch_is_as_strict_as_its_strictest_step(self):
        steps = [{"name": "mobile_take_screenshot", "arguments": {}},
                 {"name": "mobile_launch_app", "arguments": {"packageName": "com.bank.app"}},
                 {"name": "mobile_click_on_screen_at_coordinates", "arguments": {"x": 1, "y": 1}}]
        self.assertEqual(crewchat.device_action("mobile_batch_commands", {"device": "d", "steps": steps}),
                         ("launch", "com.bank.app"))
        many = [{"name": "mobile_take_screenshot", "arguments": {}}] * 150 + [
            {"name": "mobile_uninstall_app", "arguments": {"packageName": "com.a.b"}}]
        self.assertEqual(crewchat.device_action("mobile_batch_commands", {"steps": many}), ("manage", "com.a.b"))
        for steps in ([{"name": "mobile_batch_commands", "arguments": {}}], ["junk"], [], "junk"):
            self.assertEqual(crewchat.device_action("mobile_batch_commands", {"steps": steps}), ("change", ""), steps)
        reads = [{"name": "mobile_take_screenshot"}, {"name": "mobile_get_device_logs", "arguments": {"limit": 5}}]
        self.assertEqual(crewchat.device_action("mobile_batch_commands", {"steps": reads}), ("read", ""))

    def test_the_devices_a_call_uses(self):
        self.assertEqual(crewchat.device_ids("mobile_take_screenshot", {"device": "emulator-5554"}), ["emulator-5554"])
        self.assertEqual(crewchat.device_ids("mobile_list_available_devices", {}), [])
        steps = [{"name": "mobile_take_screenshot", "arguments": {}},
                 {"name": "mobile_launch_app", "arguments": {"device": "R58M", "packageName": "a.b"}},
                 {"name": "mobile_take_screenshot", "arguments": {"device": 5}}]
        self.assertEqual(crewchat.device_ids("mobile_batch_commands", {"device": "emulator-5554", "steps": steps}),
                         ["emulator-5554", "R58M"])

    def test_the_app_in_front_is_read_from_its_end(self):
        read = crewchat.front_app
        self.assertEqual(read("Foreground app: Shop (com.example.shop), activity: com.example.shop.MainActivity"),
                         "com.example.shop")
        self.assertEqual(read("Foreground app: Shop (com.example.shop)"), "com.example.shop")
        self.assertEqual(read("Foreground app: Bank (com.bank.app), activity: x.Y (com.evil.app)"), "com.evil.app")
        self.assertEqual(read("Foreground app: X (com.bank.app) (com.evil.app), activity: com.evil.app.Main"),
                         "com.evil.app")
        # iOS (Mobile MCP 1.0.16 through mobilecli): a bundle id, which may hold hyphens, and a view controller.
        self.assertEqual(read("Foreground app: Safari (com.apple.mobilesafari)"), "com.apple.mobilesafari")
        self.assertEqual(read("Foreground app: My App (com.example.my-app), activity: UIHostingController<ContentView>"),
                         "com.example.my-app")
        for text in (None, "", "Foreground app: Shop", "Foreground app: Shop (shop)", "nothing (com.a.b)",
                     "Foreground app: X (com.a.b), activity: has space"):
            self.assertIsNone(read(text), text)

class RoutineSchedule(unittest.TestCase):
    """T1: a routine's "when", in the one spelling kept, and the next time it runs (local time)."""

    def test_the_spellings(self):
        parse = crewchat.parse_schedule
        self.assertEqual(parse("every 15 minutes"), "every 15m")
        self.assertEqual(parse("Every 120m"), "every 2h")
        self.assertEqual(parse("every 1 day"), "every 1d")
        self.assertEqual(parse("daily at 9:05"), "daily 09:05")
        self.assertEqual(parse("every day 18:00"), "daily 18:00")
        self.assertEqual(parse("thu, Monday 17:30"), "mon,thu 17:30")
        self.assertEqual(parse("mon,tue,wed,thu,fri 09:00"), "weekdays 09:00")
        for bad in ("every 2m", "every 8d", "daily 24:00", "funday 09:00", "sometimes", "", None):
            with self.assertRaises(crewchat.HubError):
                parse(bad)

    def test_the_next_run(self):
        local = lambda *a: crewchat.datetime.datetime(*a).timestamp()  # noqa: E731
        monday_8 = local(2026, 11, 2, 8, 0)  # 2 Nov 2026 is a Monday
        self.assertEqual(crewchat.next_run("every 15m", monday_8), monday_8 + 900)
        self.assertEqual(crewchat.next_run("daily 09:00", monday_8), local(2026, 11, 2, 9, 0))
        self.assertEqual(crewchat.next_run("daily 09:00", local(2026, 11, 2, 9, 0)), local(2026, 11, 3, 9, 0))
        friday_10 = local(2026, 11, 6, 10, 0)
        self.assertEqual(crewchat.next_run("weekdays 09:00", friday_10), local(2026, 11, 9, 9, 0))
        self.assertEqual(crewchat.next_run("mon,thu 17:30", monday_8), local(2026, 11, 2, 17, 30))
        self.assertEqual(crewchat.next_run("sat 07:00", monday_8), local(2026, 11, 7, 7, 0))

    def test_a_cron_expression(self):
        # Owner, 20 Nov: routines on cron expressions (minute hour day-of-month month day-of-week, this machine's time)
        local = lambda *a: crewchat.datetime.datetime(*a).timestamp()  # noqa: E731
        parse, run = crewchat.parse_schedule, crewchat.next_run
        friday_10 = local(2026, 11, 20, 10, 0)  # 20 Nov 2026 is a Friday
        self.assertEqual(parse("0 9 * * 1-5"), "cron 0 9 * * 1-5")
        self.assertEqual(parse("cron 30 18 * * FRI"), "cron 30 18 * * fri")
        self.assertEqual(run("cron 0 9 * * 1-5", friday_10), local(2026, 11, 23, 9, 0))  # next Monday
        self.assertEqual(run("cron 30 18 * * fri", friday_10), local(2026, 11, 20, 18, 30))
        self.assertEqual(run("cron */15 9-17 * * mon-fri", friday_10), local(2026, 11, 20, 10, 15))
        self.assertEqual(run("cron 0 0 1 * *", friday_10), local(2026, 12, 1, 0, 0))
        self.assertEqual(run("cron 0 9 * * 0", friday_10), local(2026, 11, 22, 9, 0))  # Sunday is 0 (or 7)
        self.assertEqual(run("cron 0 9 1,15 * mon", friday_10), local(2026, 11, 23, 9, 0))  # either day, as cron does
        for bad in ("* * * * *", "*/2 * * * *", "61 9 * * *", "0 24 * * *", "0 9 * * xyz", "0 9 * * 1/0",
                    "*/00 9 * * 1-5",  # a zero step however it is written (review, 20 Nov)
                    "0 0 31 4 *", "0 0 30,31 2 *"):  # a day that never comes in its month (review, 20 Nov)
            with self.assertRaises(crewchat.HubError):
                parse(bad)
        for template in crewchat.ROUTINE_TEMPLATES:  # every ready-made routine parses and fits
            parse(template["when"])
            self.assertLessEqual(len(template["what"]), crewchat.MAX_ROUTINE_TEXT)
        self.assertEqual([t["name"] for t in crewchat.ROUTINE_TEMPLATES][:4],
                         ["jira-monitor", "jira-ticket-analyzer", "bug-triage", "ci-watch"])
        self.assertEqual(run(parse("0 0 29 2 *"), friday_10), local(2028, 2, 29, 0, 0))  # the next 29 February
        self.assertEqual(run(parse("0 0 31 4 mon"), friday_10), local(2027, 4, 5, 0, 0))  # day or weekday: April's first Monday
        triage = crewchat.parse_schedule("*/30 9-18 * * 1-5")  # every 30 minutes, 09:00 to 18:30, weekdays
        self.assertEqual(run(triage, local(2026, 11, 20, 18, 30)), local(2026, 11, 23, 9, 0))  # Friday's last, then Monday


class Routines(unittest.TestCase):
    """T1: the owner's routines post their task when due, once, and wait while the last one is open."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="routines-", dir=TMP))
        crewchat.save_config({"project": "web", "port": 9}, self.root)
        self.hub = crewchat.Hub(crewchat.Roster(self.root))

    def tearDown(self):
        self.hub.events.close()

    def routine(self, name="jira-review", when="every 30m", to="all"):
        return self.hub.set_routine(name, "Go through my Jira tickets and summarize them", to, when)

    def test_due_it_posts_its_task_once_and_waits_for_it(self):
        out = self.routine()
        start = self.hub.routines["jira-review"]["changed"]
        self.assertEqual(out["next"], start + 1800)
        self.assertEqual(self.hub.routine_tick(start + 60), [])  # not due yet
        posted = self.hub.routine_tick(start + 1800)
        self.assertEqual(len(posted), 1)
        row = self.hub.sheet[posted[0]]
        self.assertEqual(row["title"], "Routine jira-review: Go through my Jira tickets and summarize them")
        self.assertEqual(self.hub.by_id[posted[0]]["routine"], "jira-review")
        self.assertEqual(self.hub.routine_tick(start + 1860), [])  # once
        # Due again while its task is still open: it waits, and says so once.
        last = self.hub.routines["jira-review"]["last"]
        self.assertEqual(self.hub.routine_tick(last + 1800), [])
        self.assertEqual(self.hub.routine_tick(last + 1860), [])
        waits = [m for m in self.hub.messages if m["kind"] == "event" and "waits" in m["text"]]
        self.assertEqual(len(waits), 1)
        self.assertEqual(self.hub.routines_view()["routines"][0]["waits"], posted[0])
        self.hub.owner_status(posted[0], "done", "")
        again = self.hub.routine_tick(last + 1920)
        self.assertEqual(len(again), 1)  # finished: the late run goes out
        self.assertEqual(self.hub.routines_view()["routines"][0]["last_task"], again[0])

    def test_for_an_agent_or_the_lead_it_is_theirs_at_once(self):
        sid = self.hub.open_session("mac", "claude")
        name = self.hub.agent_for(sid)
        self.hub.set_role(crewchat.OWNER, name, "lead")
        self.routine("standup", "every 5m", "lead")
        due = self.hub.routines["standup"]["changed"] + 300
        mid = self.hub.routine_tick(due)[0]
        self.assertEqual(self.hub.taken[mid], name)
        self.routine("triage", "every 5m", name)
        mid = self.hub.routine_tick(due + 600)[-1]
        self.assertEqual(self.hub.by_id[mid]["to"], name)
        with self.assertRaises(crewchat.HubError):
            self.routine("nobody", "every 5m", "ghost")  # not an agent here

    def test_with_no_lead_its_task_is_on_the_sheet(self):
        self.routine("standup", "every 5m", "lead")
        mid = self.hub.routine_tick(self.hub.routines["standup"]["changed"] + 300)[0]
        self.assertEqual(self.hub.by_id[mid]["to"], "all")
        self.assertNotIn(mid, self.hub.taken)

    def test_paused_changed_and_removed(self):
        self.routine()
        self.hub.set_routine("jira-review", on=False)
        far = time.time() + 86400
        self.assertEqual(self.hub.routine_tick(far), [])
        self.hub.set_routine("jira-review", on=True, when="daily 09:00")
        view = self.hub.routines_view()["routines"][0]
        self.assertEqual((view["when"], view["on"]), ("daily 09:00", True))
        self.assertEqual(view["what"], "Go through my Jira tickets and summarize them")  # kept
        self.hub.remove_routine("jira-review")
        self.assertEqual(self.hub.routines_view()["routines"], [])
        self.assertEqual(self.hub.routine_tick(far * 2), [])
        with self.assertRaises(crewchat.HubError):
            self.hub.run_routine("jira-review")
        for bad in (("---", "x", None, "every 5m"), ("ok", "", None, "every 5m"), ("ok", "x", None, "now")):
            with self.assertRaises(crewchat.HubError):
                self.hub.set_routine(*bad)
        # A name as typed is kept in the routines' spelling (owner, 18 Nov: Save refused "jira ticket lookup")
        self.hub.set_routine("Jira Ticket Lookup", "x", None, "every 30 min")
        self.assertIn("jira-ticket-lookup", [r["name"] for r in self.hub.routines_view()["routines"]])
        # ...and found by it as typed, to run or remove (release review, 20 Nov: "no routine called jira ticket lookup")
        self.assertTrue(self.hub.run_routine("Jira Ticket Lookup", force=True))
        self.hub.remove_routine("Jira Ticket Lookup")
        self.assertNotIn("jira-ticket-lookup", [r["name"] for r in self.hub.routines_view()["routines"]])

    def test_run_now_posts_even_while_the_last_is_open(self):
        self.routine()
        first = self.hub.run_routine("jira-review", force=True)
        self.assertEqual(self.hub.run_routine("jira-review"), "")
        self.assertNotEqual(self.hub.run_routine("jira-review", force=True), first)

    def test_only_the_coordinating_machine_posts(self):
        self.routine()
        due = self.hub.routines["jira-review"]["changed"] + 1800
        self.hub.sync = types.SimpleNamespace(coordinator=lambda: {"me": False}, publish=lambda msg: None,
                                               state_changed=lambda: None, device_id="m2")
        try:
            self.assertEqual(self.hub.routine_tick(due), [])
            self.assertFalse(self.hub.routines_view()["coordinating"])
            self.hub.sync.coordinator = lambda: {"me": True}
            self.assertEqual(len(self.hub.routine_tick(due)), 1)
        finally:
            self.hub.sync = None

    def test_routines_and_runs_travel_to_other_machines_and_survive_a_restart(self):
        self.routine()
        defined = next(m for m in self.hub.messages if m["kind"] == "plan" and m.get("routine"))
        run = self.hub.run_routine("jira-review", force=True)
        root = Path(tempfile.mkdtemp(prefix="routines-other-", dir=TMP))
        crewchat.save_config({"project": "web", "port": 9}, root)
        other = crewchat.Hub(crewchat.Roster(root))
        try:
            self.assertTrue(other.ingest(dict(defined, id="A1")))
            self.assertTrue(other.ingest(dict(self.hub.by_id[run], id="A2")))
            routine = other.routines["jira-review"]
            self.assertEqual((routine["when"], routine["last_task"]), ("every 30m", "A2"))
            older = json.dumps({"name": "jira-review", "what": "old", "when": "every 5m"})
            other.ingest(dict(defined, id="A3", ts=defined["ts"] - 60, routine=older))  # an older change, late
            self.assertEqual(other.routines["jira-review"]["when"], "every 30m")
        finally:
            other.events.close()
        with self.hub.lock:
            self.hub._save()
        again = crewchat.Hub(crewchat.Roster(self.root))
        try:
            self.assertEqual(again.routines["jira-review"]["last_task"], run)
        finally:
            again.events.close()
        state = json.loads((self.root / "state.json").read_text())
        del state["routines"]  # as an older crewchat wrote it: read back from the log
        (self.root / "state.json").write_text(json.dumps(state))
        replayed = crewchat.Hub(crewchat.Roster(self.root))
        try:
            self.assertEqual(replayed.routines["jira-review"]["last_task"], run)
        finally:
            replayed.events.close()

    def test_a_routine_message_not_from_the_owner_changes_nothing(self):
        # A routine can start an agent with every permission (T4): only the owner's routine message counts, as an
        # agent's or one posted under another name from a linked device would not.
        routine = json.dumps({"name": "sneaky", "what": "run anything", "when": "every 5m",
                              "start": {"tool": "claude", "folder": str(self.root), "permissions": "all"}})
        self.assertTrue(self.hub.ingest({"id": "X1", "ts": time.time(), "from": "claude-mac", "to": "all", "text": "",
                                         "kind": "plan", "routine": routine}))
        self.assertNotIn("sneaky", self.hub.routines)
        self.hub.ingest({"id": "X2", "ts": time.time(), "from": crewchat.OWNER, "to": "all", "text": "", "kind": "plan",
                         "routine": routine})
        self.assertIn("sneaky", self.hub.routines)


class RoutinesFromAnotherMachine(unittest.TestCase):
    """The owner's decision (14 Nov 2026): a routine set on another linked machine posts nothing and starts no agent
    here until the owner approves it on this machine, and again after each change to what it does."""

    def setUp(self):
        root = Path(tempfile.mkdtemp(prefix="routines-held-", dir=TMP))
        crewchat.save_config({"project": "web", "port": 9}, root)
        self.root = root
        self.hub = crewchat.Hub(crewchat.Roster(root))
        self.addCleanup(self.hub.events.close)

    def arrive(self, mid, what="Go through my Jira tickets", when="every 30m", start=None, on=True, ts=None):
        data = {"name": "jira-review", "what": what, "to": "all", "when": when, "on": on}
        if start:
            data["start"] = start
        self.hub.ingest({"id": mid, "ts": ts or time.time(), "from": crewchat.OWNER, "to": "all", "text": "",
                         "kind": "plan", "origin": "mac-a", "routine": json.dumps(data)})
        return [n for n in self.hub.needs_you() if n["kind"] == "shared"]

    def test_it_waits_for_approval_here_then_runs_and_a_change_asks_again(self):
        start = {"tool": "claude", "folder": str(self.root), "permissions": "all"}
        [item] = self.arrive("A1", start=start)
        self.assertIn("Go through my Jira tickets", item["text"])
        self.assertIn("permissions: all", item["text"])
        self.assertIn(str(self.root), item["text"])
        due = self.hub.routines["jira-review"]["changed"] + 1800
        with mock.patch.object(self.hub, "launch") as launch:
            self.assertEqual(self.hub.routine_tick(due), [])  # nothing posted, no agent started
            with self.assertRaises(crewchat.HubError):
                self.hub.run_routine("jira-review", force=True)  # not even Run now
            launch.assert_not_called()
        self.assertEqual([i for i in self.hub.shared_needs() if i["kind"] == "shared"], [])
        self.assertTrue(self.hub.routines_view()["routines"][0]["held"])
        crewchat.answer_need(self.hub, {"key": item["key"], "answer": "approve"})
        self.assertEqual([n for n in self.hub.needs_you() if n["kind"] == "shared"], [])
        with mock.patch.object(self.hub, "launch") as launch:
            [posted] = self.hub.routine_tick(due)
            launch.assert_called_once()
        self.hub.owner_status(posted, "done", "")
        # Paused and resumed there: nothing to approve again.
        self.assertEqual(self.arrive("A2", start=start, on=False), [])
        self.assertEqual(self.arrive("A3", start=start), [])
        # What it does changed there: held again, and it does not run meanwhile.
        [again] = self.arrive("A4", what="Delete every branch", start=start)
        self.assertIn("Delete every branch", again["text"])
        with mock.patch.object(self.hub, "launch") as launch:
            self.assertEqual(self.hub.routine_tick(time.time() + 7 * 86400), [])
            crewchat.answer_need(self.hub, {"key": again["key"], "answer": "decline"})
            self.assertEqual([n for n in self.hub.needs_you() if n["kind"] == "shared"], [])
            self.assertEqual(self.hub.routine_tick(time.time() + 14 * 86400), [])  # declined: not run here
            launch.assert_not_called()
        self.assertTrue(self.hub.routines_view()["routines"][0]["declined"])

    def test_one_set_on_this_machine_runs_without_asking(self):
        self.hub.set_routine("jira-review", "Go through my Jira tickets", "all", "every 30m")
        self.assertEqual([n for n in self.hub.needs_you() if n["kind"] == "shared"], [])
        self.assertEqual(len(self.hub.routine_tick(self.hub.routines["jira-review"]["changed"] + 1800)), 1)

    def test_routines_from_before_the_update_are_sorted_by_where_they_came_from(self):
        self.hub.set_routine("mine", "Summarize the day", "all", "every 30m")
        self.arrive("A1")
        self.hub.routine_ok.clear()  # as a crewchat before 2.0.1 left them
        self.hub.held_checked = False
        far = time.time() + 7 * 86400
        self.hub.sync = types.SimpleNamespace(coordinator=lambda: {"me": True}, publish=lambda msg: None,
                                              state_changed=lambda: None, device_id="mac-b")
        try:
            posted = self.hub.routine_tick(far)
        finally:
            self.hub.sync = None
        self.assertEqual([self.hub.by_id[m]["routine"] for m in posted], ["mine"])
        self.assertEqual([n["name"] for n in self.hub.needs_you() if n["kind"] == "shared"], ["jira-review"])


class AskReceipts(unittest.TestCase):
    """W6's receipts in the poll for an ask whose receiver has no cursor here: the page and the phone's snapshot keep
    working."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="ask-receipts-", dir=TMP))
        crewchat.save_config({"project": "web", "port": 9}, self.root)
        self.hub = crewchat.Hub(crewchat.Roster(self.root))
        self.name = self.hub.agent_for(self.hub.open_session("mac", "claude"))

    def tearDown(self):
        self.hub.events.close()

    def asks(self):
        return self.hub.poll(-1, -1, 0)["asks"]

    def test_an_ask_to_an_agent_on_another_machine(self):
        self.assertTrue(self.hub.ingest({"id": "R1", "ts": time.time(), "from": crewchat.OWNER, "to": "codex-far",
                                         "text": "Ready?", "kind": "msg", "ask": "1", "origin": "m2"}))
        self.assertEqual(self.asks()["R1"], "sent")
        with self.hub.lock:
            self.assertEqual(self.hub._ask_remind("codex-far", time.time()), "")

    def test_an_ask_to_an_agent_renamed_since(self):
        mid = self.hub.send(crewchat.OWNER, self.name, "Which branch?", ask=True)["id"]
        with self.hub.lock:
            self.hub.handed[self.name] = 10 ** 6  # its hooks handed it over
        self.hub.rename(self.name, "renamed-agent")
        self.assertEqual(self.asks()[mid], "handed")  # the receipt follows it to its new name
        with self.hub.lock:
            self.assertEqual(self.hub.open_asks[mid]["to"], "renamed-agent")
            self.assertNotIn(self.name, self.hub.handed)
            self.assertEqual(self.hub.by_id[mid]["to"], self.name)  # the message itself is as it was sent
        self.hub.send("renamed-agent", crewchat.OWNER, "main")  # and its answer under the new name answers it
        self.assertEqual(self.asks()[mid], "answered")

    def test_an_ask_to_an_agent_removed_since(self):
        mid = self.hub.send(crewchat.OWNER, self.name, "Still there?", ask=True)["id"]
        with self.hub.lock:
            self.hub.handed[self.name] = 1
        self.hub.remove(self.name)
        self.assertEqual(self.asks()[mid], "sent")
        with self.hub.lock:
            self.assertNotIn(self.name, self.hub.handed)


class RoutineStartsAgent(unittest.TestCase):
    """T4: a routine due while no agent that could take it is here starts one session (a folder, tool and permissions
    chosen on the routine, as New agent starts one), at most one per due run and at most the owner's cap a day."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="routine-starts-", dir=TMP))
        crewchat.save_config({"project": "web", "port": 9}, self.root)
        self.hub = crewchat.Hub(crewchat.Roster(self.root))
        self.folder = str(Path(tempfile.mkdtemp(prefix="routine-folder-", dir=TMP)).resolve())
        self.launched = []
        self.saved = crewchat.launch_agent, crewchat.first_run_questions
        crewchat.launch_agent = lambda tool, folder, key, permissions="ask", root=None, model="", env=None, \
            chrome=False, background=False, fork="": self.launched.append((tool, folder, key, permissions, model)) or ""
        crewchat.first_run_questions = lambda tool, folder, env=None: []
        connected = mock.patch.object(self.hub, "_connected_folder", side_effect=lambda f: f == self.folder)
        connected.start()
        self.addCleanup(connected.stop)

    def tearDown(self):
        crewchat.launch_agent, crewchat.first_run_questions = self.saved
        self.hub.events.close()

    def routine(self, name="nightly", to="all", start=True, permissions="ask", when="every 30m"):
        return self.hub.set_routine(name, "Fix the failing tests", to, when,
                                    start={"tool": "claude", "folder": self.folder, "permissions": permissions}
                                    if start else None)

    def due(self, name="nightly"):
        return self.hub._routine_next(self.hub.routines[name])

    def events(self, word):
        return [m["text"] for m in self.hub.messages if m["kind"] == "event" and word in m["text"]]

    def test_with_nobody_here_it_starts_one_agent_that_gets_the_task(self):
        out = self.routine(permissions="edits")
        self.assertEqual(out["start"], {"tool": "claude", "folder": self.folder, "permissions": "edits", "model": ""})
        due = self.due()
        mid = self.hub.routine_tick(due)[0]
        self.assertEqual(len(self.launched), 1)
        tool, folder, key, permissions, _ = self.launched[0]
        self.assertEqual((tool, folder, permissions), ("claude", self.folder, "edits"))
        self.assertEqual(self.hub.by_id[mid]["to"], "all")  # on the sheet, for the agent it starts
        self.assertTrue(any("Routine nightly started a new Claude Code agent" in t for t in self.events("started")))
        # At most one per due run: due again before it checks in (its task is open), and Run now, start no other.
        self.assertEqual(self.hub.routine_tick(self.due() + 3600), [])
        self.hub.run_routine("nightly", force=True)
        self.assertEqual(len(self.launched), 1)
        # It checks in with its key and the routine's task is its own.
        sid = self.hub.open_session("mac", "claude")
        name, note = self.hub.start_link(sid, key)
        self.assertEqual(self.hub.taken[mid], name)
        self.assertIn("your first task", note.lower())
        with mock.patch.object(crewchat.time, "time", return_value=due):  # "today" is the day it ran (due may be tomorrow)
            view = self.hub.routines_view()
        self.assertEqual((view["started_today"], view["start_cap"]), (1, 10))

    def test_an_agent_that_could_take_it_is_here_so_none_is_started(self):
        self.routine()
        sid = self.hub.open_session("mac", "claude")
        self.hub.agent_for(sid)  # seen just now: here
        self.assertEqual(len(self.hub.routine_tick(self.due())), 1)
        self.assertEqual(self.launched, [])

    def test_for_an_agent_who_is_not_here_one_is_started(self):
        sid = self.hub.open_session("mac", "claude")
        name = self.hub.agent_for(sid)
        self.routine(to=name)
        with self.hub.lock:
            self.hub.agents[name]["seen"] = 0  # gone for the night
        mid = self.hub.routine_tick(self.due())[0]
        self.assertEqual(len(self.launched), 1)
        self.assertEqual(self.hub.by_id[mid]["to"], "all")  # not left with the one who is away

    def test_no_start_set_posts_as_before(self):
        self.routine(start=False)
        self.assertEqual(len(self.hub.routine_tick(self.due())), 1)
        self.assertEqual(self.launched, [])

    def test_the_daily_cap(self):
        self.assertEqual(self.hub.routines_view()["start_cap"], 10)  # the owner's default (4 Nov decision: 10)
        self.hub.set_routine_cap(1)
        self.routine("one")
        self.routine("two")
        now = max(self.due("one"), self.due("two"))
        self.assertEqual(len(self.hub.routine_tick(now)), 2)  # both posted
        self.assertEqual(len(self.launched), 1)  # one started
        self.assertEqual(len(self.events("did not start an agent")), 1)  # the other says why not
        # The next day counts again.
        for name in ("one", "two"):
            self.hub.owner_status(self.hub.routines[name]["last_task"], "done", "")
        self.hub.routine_tick(now + 86400)
        self.assertEqual(len(self.launched), 2)
        self.hub.set_routine_cap(0)  # none at all
        for name in ("one", "two"):
            self.hub.owner_status(self.hub.routines[name]["last_task"], "done", "")
        self.hub.routine_tick(now + 3 * 86400)
        self.assertEqual(len(self.launched), 2)
        for bad in (-1, 51, "lots"):
            with self.assertRaises(crewchat.HubError):
                self.hub.set_routine_cap(bad)

    def test_what_a_start_may_be(self):
        self.assertEqual(self.routine(permissions="all")["start"]["permissions"], "all")  # owner: any mode
        for bad in ({"tool": "nope", "folder": self.folder}, {"tool": "claude", "folder": "/not/connected"},
                    {"tool": "claude", "folder": self.folder, "permissions": "yolo"}):
            with self.assertRaises(crewchat.HubError):
                self.hub.set_routine("nightly", start=bad)
        self.assertIn("start", self.hub.routines_view()["routines"][0])
        self.hub.set_routine("nightly", start={})  # cleared
        self.assertNotIn("start", self.hub.routines_view()["routines"][0])

    def test_a_folder_not_here_or_a_launch_that_fails_leaves_the_task_on_the_sheet(self):
        self.routine()
        def boom(*a, **k):
            raise crewchat.HubError("Claude Code is not installed")
        crewchat.launch_agent = boom
        mid = self.hub.routine_tick(self.due())[0]
        self.assertIn(mid, self.hub.sheet)
        self.assertTrue(self.events("could not start an agent"))
        self.assertEqual(self.hub.routines_view()["started_today"], 0)

    def test_the_start_travels_with_the_routine(self):
        self.routine(permissions="auto")
        defined = [m for m in self.hub.messages if m["kind"] == "plan" and m.get("routine")][-1]
        root = Path(tempfile.mkdtemp(prefix="routine-starts-other-", dir=TMP))
        crewchat.save_config({"project": "web", "port": 9}, root)
        other = crewchat.Hub(crewchat.Roster(root))
        try:
            other.ingest(dict(defined, id="A1"))
            self.assertEqual(other.routines["nightly"]["start"]["permissions"], "auto")
        finally:
            other.events.close()


class RoutinesOverHttp(Base):
    """T2: the owner's routines from the page and the command line (/api/routines); agents cannot reach it."""

    def test_the_owner_adds_runs_pauses_and_removes_one(self):
        call = lambda body: crewchat.post_json(self.base + "/api/routines", self.owner, body)  # noqa: E731
        view = call({"op": "view"})
        self.assertTrue(view["coordinating"])
        self.assertIn("jira-review", [t["name"] for t in view["templates"]])
        try:
            out = cli("routines", "add", "pr-check", "Look", "at", "open", "PRs", "--when", "every 2h")
            self.assertIn("Routine pr-check: every 2h, next", out)
            self.assertIn("pr-check", cli("routines"))
            mid = call({"op": "run", "name": "pr-check"})["id"]
            self.assertIn("Routine pr-check: Look at open PRs", next(r for r in self.hub.sheet_rows() if r["id"] == mid)["title"])
            self.assertIn("paused", cli("routines", "pause", "pr-check"))
            self.assertFalse(call({})["routines"][0]["on"])
            self.assertIn("weekdays 09:00", cli("routines", "templates"))
        finally:
            cli("routines", "remove", "pr-check")
        self.assertEqual(call({})["routines"], [])
        with self.assertRaises(urllib.error.HTTPError) as e:
            crewchat.post_json(self.base + "/api/routines", self.token, {"op": "view"})  # an agent's place token
        self.assertIn(e.exception.code, (401, 403))

    def test_the_page_has_the_routines_card(self):
        for part in ('id="routines-card"', 'id="routine-template"', 'postJSON("/api/routines"', "loadRoutines();",
                     'id="jira-every"', 'id="routine-start-tool"', 'id="routine-start-folder"',
                     'id="routine-start-permissions"', 'id="routine-cap"', 'op: "cap"'):
            self.assertIn(part, crewchat.CHAT_PAGE)


class JiraEvery(unittest.TestCase):
    """T3: the owner sets how often the look in Jira runs; it runs no more often than that."""

    def setUp(self):
        root = Path(tempfile.mkdtemp(prefix="jira-every-", dir=TMP))
        crewchat.save_config({"project": "web", "port": 9}, root)
        self.hub = crewchat.Hub(crewchat.Roster(root))

    def tearDown(self):
        self.hub.events.close()

    def test_the_interval(self):
        self.assertEqual(self.hub.jira_watch()["every"], 5)
        self.assertEqual(self.hub.set_jira_every("30")["every"], 30)
        for bad in ("2", "2000", "soon"):
            with self.assertRaises(crewchat.HubError):
                self.hub.set_jira_every(bad)
        with mock.patch.object(self.hub, "jira_poll", return_value=["A1"]) as poll:
            self.assertEqual(self.hub.jira_poll_if_due(1000000), ["A1"])
            self.assertEqual(self.hub.jira_poll_if_due(1000000 + 29 * 60), [])
            self.assertEqual(self.hub.jira_poll_if_due(1000000 + 30 * 60), ["A1"])
            self.assertEqual(poll.call_count, 2)


if __name__ == "__main__":
    unittest.main()
