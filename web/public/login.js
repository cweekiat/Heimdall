// Access-code sign-in (task 062). Plain script: it must work before the app bundle is reachable.
(function () {
  "use strict";
  var f = document.getElementById("f");
  var code = document.getElementById("code");
  var go = document.getElementById("go");
  var msg = document.getElementById("msg");
  fetch("/api/session", { credentials: "same-origin" })
    .then(function (r) { return r.json(); })
    .then(function (s) { if (!s.auth || s.signed_in) location.replace("/"); })
    .catch(function () {});
  f.addEventListener("submit", function (e) {
    e.preventDefault();
    var v = code.value.trim();
    if (!v) { msg.textContent = "Enter your access code."; return; }
    go.disabled = true;
    msg.textContent = "";
    fetch("/api/login", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code: v }),
    })
      .then(function (r) {
        if (r.ok) { location.replace("/"); return; }
        return r.json().catch(function () { return {}; }).then(function (b) {
          msg.textContent = b.detail || "Sign-in failed (" + r.status + ").";
          code.select();
        });
      })
      .catch(function () { msg.textContent = "Network error. Try again."; })
      .finally(function () { go.disabled = false; });
  });
})();
