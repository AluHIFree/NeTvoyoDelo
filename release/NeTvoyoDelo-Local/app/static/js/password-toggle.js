/**
 * Глазик «показать/скрыть пароль» для полей с data-password-toggle
 * или обёрток .password-field / .input-icon с input[type=password].
 */
(function () {
  const EYE_OPEN =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">' +
    '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/>' +
    '<circle cx="12" cy="12" r="3"/>' +
    "</svg>";
  const EYE_OFF =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">' +
    '<path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94"/>' +
    '<path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19"/>' +
    '<path d="M14.12 14.12a3 3 0 1 1-4.24-4.24"/>' +
    '<line x1="1" y1="1" x2="23" y2="23"/>' +
    "</svg>";

  function ensureToggle(input) {
    if (!input || input.dataset.toggleBound === "1") return;
    if (input.type !== "password" && input.type !== "text") return;

    let wrap = input.closest(".password-field, .input-icon");
    if (!wrap) {
      wrap = document.createElement("div");
      wrap.className = "password-field";
      input.parentNode.insertBefore(wrap, input);
      wrap.appendChild(input);
    }
    wrap.classList.add("has-password-toggle");

    if (wrap.querySelector(".password-toggle")) {
      input.dataset.toggleBound = "1";
      return;
    }

    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "password-toggle";
    btn.setAttribute("aria-label", "Показать пароль");
    btn.setAttribute("tabindex", "0");
    btn.innerHTML = EYE_OPEN;

    btn.addEventListener("click", function () {
      const showing = input.type === "text";
      input.type = showing ? "password" : "text";
      btn.innerHTML = showing ? EYE_OPEN : EYE_OFF;
      btn.setAttribute(
        "aria-label",
        showing ? "Показать пароль" : "Скрыть пароль"
      );
      btn.classList.toggle("is-visible", !showing);
      input.focus({ preventScroll: true });
    });

    wrap.appendChild(btn);
    input.dataset.toggleBound = "1";
  }

  function init(root) {
    const scope = root || document;
    scope
      .querySelectorAll(
        'input[type="password"][data-password-toggle], .password-field input[type="password"], .input-icon input[type="password"]'
      )
      .forEach(ensureToggle);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      init();
    });
  } else {
    init();
  }

  window.initPasswordToggles = init;
})();
