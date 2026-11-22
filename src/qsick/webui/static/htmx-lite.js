/*
 * htmx-lite: the subset of htmx QSICK uses, vendored so the console works
 * offline. Supported attributes:
 *
 *   hx-get="url"  hx-trigger="every 2s" | "click"  hx-target="css"  hx-swap="innerHTML"
 *
 * If real htmx is ever loaded first, this file steps aside.
 */
(() => {
  "use strict";
  if (window.htmx) return;

  const api = {
    version: "qsick-lite",
    process(root = document) {
      root.querySelectorAll("[hx-get]").forEach((el) => {
        if (el.dataset.qsickBound) return;
        el.dataset.qsickBound = "1";
        const trigger = el.getAttribute("hx-trigger") || "click";
        const every = /^every\s+(\d+(?:\.\d+)?)\s*(ms|s)$/i.exec(trigger.trim());
        if (every) {
          const ms = every[2].toLowerCase() === "s" ? parseFloat(every[1]) * 1000 : parseFloat(every[1]);
          const tick = () => request(el).catch(() => {});
          setTimeout(tick, ms);
          setInterval(tick, Math.max(250, ms));
        } else if (trigger.includes("click")) {
          el.addEventListener("click", (event) => {
            if (el.tagName === "FORM") event.preventDefault();
            request(el).catch(() => {});
          });
        }
        // Immediate load when asked to.
        if (trigger.includes("load")) request(el).catch(() => {});
      });
    },
  };

  async function request(el) {
    const url = el.getAttribute("hx-get");
    const target = el.getAttribute("hx-target");
    const swap = el.getAttribute("hx-swap") || "innerHTML";
    const node = target ? document.querySelector(target) : el;
    if (!url || !node) return;
    const response = await fetch(url, { headers: { "HX-Request": "true" } });
    if (!response.ok) return;
    const html = await response.text();
    if (swap === "outerHTML") node.outerHTML = html;
    else node.innerHTML = html;
  }

  window.htmx = api;
  document.addEventListener("DOMContentLoaded", () => api.process());
  document.body && api.process();
})();
