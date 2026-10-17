// crewchat's website: Copy buttons on commands, and demo videos that play only while on screen
// (and not at all for readers who ask for less motion: they get the poster and the controls).
(function () {
  "use strict";

  document.querySelectorAll(".code .copy").forEach(function (button) {
    if (!navigator.clipboard) return;
    button.hidden = false;
    button.addEventListener("click", function () {
      var text = button.parentElement.querySelector("code").textContent;
      navigator.clipboard.writeText(text).then(function () {
        button.textContent = "Copied";
        setTimeout(function () { button.textContent = "Copy"; }, 1600);
      });
    });
  });

  var videos = document.querySelectorAll(".clip video");
  var still = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (still) {
    videos.forEach(function (v) { v.removeAttribute("autoplay"); v.pause(); v.controls = true; });
    return;
  }
  if (!("IntersectionObserver" in window)) return;
  var seen = new IntersectionObserver(function (entries) {
    entries.forEach(function (e) {
      if (e.isIntersecting) {
        var playing = e.target.play();
        if (playing && playing.catch) playing.catch(function () { e.target.controls = true; });
      } else {
        e.target.pause();
      }
    });
  }, { threshold: 0.25 });
  videos.forEach(function (v) { seen.observe(v); });
})();
