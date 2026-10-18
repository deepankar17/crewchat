// The public demo of crewchat's analytics view (roadmap X3): the very view the app has, run in the browser on
// its built-in sample data. No server and no sign-in; nothing is sent anywhere. The view uses a few of the chat
// page's helpers and its state: here are small stand-ins for them.
window.crewDemoOnly = true;
window.crewPictures = "/demo/pictures/";
var state = { agents: [], sheet: [], link: null, device: "", now: Date.now() / 1000, roles: [], tools: {}, presets: {},
              art: { office: 1, coder: 1, technologist: 1, scientist: 1, mechanic: 1, detective: 1, teacher: 1, artist: 1 } };
function $(id) { return document.getElementById(id); }
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined && text !== null) e.textContent = text;
  return e;
}
function initials(name) {
  return String(name).split(/[\s._-]+/).filter(Boolean).slice(0, 2).map((p) => p[0].toUpperCase()).join("") || "?";
}
function avatar(a) {
  const box = el("span", "avatar"), [kind, id] = (a.picture || "").split(":");
  if (kind === "art" && state.art[id]) {
    const img = el("img", "art");
    img.alt = ""; img.src = window.crewPictures + id + ".webp";
    box.append(img);
  } else {
    box.append(el("span", "initials", initials(a.agent)));
  }
  box.title = a.agent;
  return box;
}
function postJSON() { return Promise.reject(new Error("the demo has no server")); }
function openPanel() {}
function showView() {}
document.addEventListener("DOMContentLoaded", () => {
  crewAnalytics.show($("analytics"));
  crewAnalytics.demo(true);
});
