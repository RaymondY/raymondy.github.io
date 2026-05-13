// Tiny site script. No dependencies.
// Handles: theme toggle, mobile nav, and the "Show N more" news toggle.

(function () {
  "use strict";

  // ---------- Theme toggle -------------------------------------------------
  var root = document.documentElement;

  function currentTheme() {
    return root.getAttribute("data-theme") ||
      (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  }

  function setTheme(t) {
    root.setAttribute("data-theme", t);
    try { localStorage.setItem("theme", t); } catch (e) {}
    syncThemeIcons(t);
  }

  function syncThemeIcons(t) {
    document.querySelectorAll("[data-theme-toggle]").forEach(function (btn) {
      var sun = btn.querySelector(".theme-icon--sun");
      var moon = btn.querySelector(".theme-icon--moon");
      if (!sun || !moon) return;
      if (t === "dark") { sun.hidden = true;  moon.hidden = false; }
      else              { sun.hidden = false; moon.hidden = true;  }
    });
  }

  document.querySelectorAll("[data-theme-toggle]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      setTheme(currentTheme() === "dark" ? "light" : "dark");
    });
  });
  syncThemeIcons(currentTheme());

  // ---------- Mobile nav ---------------------------------------------------
  document.querySelectorAll("[data-nav-toggle]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var list = document.getElementById(btn.getAttribute("aria-controls") || "nav-list");
      if (!list) return;
      var open = list.classList.toggle("is-open");
      btn.setAttribute("aria-expanded", open ? "true" : "false");
    });
  });

  // Close mobile nav on link click
  document.querySelectorAll(".nav__list a").forEach(function (a) {
    a.addEventListener("click", function () {
      var list = a.closest(".nav__list");
      if (list && list.classList.contains("is-open")) {
        list.classList.remove("is-open");
        var toggle = document.querySelector("[data-nav-toggle]");
        if (toggle) toggle.setAttribute("aria-expanded", "false");
      }
    });
  });

  // ---------- Avatar flip (mobile tap; desktop uses :hover) ---------------
  document.querySelectorAll("[data-avatar-flip]").forEach(function (wrap) {
    function toggle() { wrap.classList.toggle("is-flipped"); }
    wrap.addEventListener("click", toggle);
    wrap.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
    });
  });

  // ---------- News "show more" --------------------------------------------
  document.querySelectorAll("[data-news-toggle]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var list = btn.previousElementSibling;
      if (!list || !list.classList.contains("news")) return;
      var collapsed = list.getAttribute("data-collapsed") === "true";
      list.setAttribute("data-collapsed", collapsed ? "false" : "true");
      btn.textContent = collapsed
        ? btn.getAttribute("data-label-less")
        : btn.getAttribute("data-label-more");
    });
  });
})();
