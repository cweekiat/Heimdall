// Project page behaviour: videos play only when motion is welcome, and the copy button.
(function () {
  "use strict";
  var still = window.matchMedia("(prefers-reduced-motion: reduce)");
  var videos = Array.prototype.slice.call(document.querySelectorAll("video.motion"));

  // Muted autoplay while on screen, only if Reduce Motion is off. Native controls always work,
  // and once someone pauses a video it stays paused.
  videos.forEach(function (v) {
    v.addEventListener("pause", function () { if (!v._auto) v._held = true; v._auto = false; });
  });
  function autoPause(v) { if (!v.paused) { v._auto = true; v.pause(); } }
  function autoPlay(v) { if (!still.matches && !v._held) v.play().catch(function () {}); }

  if ("IntersectionObserver" in window) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) { e.isIntersecting ? autoPlay(e.target) : autoPause(e.target); });
    }, { threshold: 0.35 });
    videos.forEach(function (v) { io.observe(v); });
  }
  function onMotionChange() { if (still.matches) videos.forEach(autoPause); }
  if (still.addEventListener) still.addEventListener("change", onMotionChange);

  // Copy the quick-start commands.
  var btn = document.querySelector("[data-copy]");
  var out = document.getElementById("copied");
  if (btn && navigator.clipboard) {
    btn.addEventListener("click", function () {
      var text = document.getElementById(btn.getAttribute("data-copy")).innerText;
      navigator.clipboard.writeText(text).then(function () {
        out.textContent = "Copied to the clipboard.";
        btn.textContent = "Copied";
        setTimeout(function () { btn.textContent = "Copy"; out.textContent = ""; }, 2500);
      }, function () { out.textContent = "Couldn't copy. Select the text and copy it instead."; });
    });
  } else if (btn) {
    btn.hidden = true;
  }
})();
