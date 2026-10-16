// crewchat's website: Copy buttons on commands, and demo videos that play only while on screen
// (and not at all for readers who ask for less motion: they get the poster and the controls).
(function () {
  "use strict";

  // Firebase's own addresses (crewchat-chat.web.app) forward to the site's, as the canonical link names
  // it; a preview channel (crewchat-chat--preview-xyz.web.app) stays where it is.
  var canonical = document.querySelector('link[rel="canonical"]');
  var host = location.hostname;
  if (canonical && host.indexOf("--") === -1 && /(^|\.)(web\.app|firebaseapp\.com)$/.test(host)) {
    var to = new URL(canonical.href);
    if (to.hostname !== host) {
      location.replace(to.origin + location.pathname + location.search + location.hash);
      return;
    }
  }

  if (navigator.clipboard) document.querySelectorAll(".code .copy").forEach(function (button) {
    button.hidden = false;
    button.addEventListener("click", function () {
      var text = button.parentElement.querySelector("code").textContent;
      navigator.clipboard.writeText(text).then(function () {
        button.textContent = "Copied";
        setTimeout(function () { button.textContent = "Copy"; }, 1600);
      });
    });
  });

  var videos = document.querySelectorAll("video.clip");
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    videos.forEach(function (v) { v.controls = true; });  // they play only if asked to
    return;
  }
  if (!("IntersectionObserver" in window)) return;
  var seen = new IntersectionObserver(function (entries) {
    entries.forEach(function (e) {
      if (e.isIntersecting) {
        e.target.play().catch(function () { e.target.controls = true; });
      } else {
        e.target.pause();
      }
    });
  }, { threshold: 0.25 });
  videos.forEach(function (v) { seen.observe(v); });
})();
