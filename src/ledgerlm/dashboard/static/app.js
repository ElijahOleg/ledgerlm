/* Chart bootstrap: every <canvas data-endpoint=...> fetches its JSON fragment
 * (same-origin, localhost only) and renders a Chart.js chart. Re-runs after
 * every HTMX swap so filter changes re-render charts from fresh fragments. */
(function () {
  "use strict";

  var registry = {}; // canvas id -> Chart instance

  function cssVar(name, fallback) {
    var v = getComputedStyle(document.documentElement).getPropertyValue(name);
    return (v || fallback).trim();
  }

  function money(v) {
    return "$" + Number(v).toLocaleString(undefined, {
      minimumFractionDigits: 2,
      maximumFractionDigits: 4,
    });
  }

  function baseOptions(horizontal) {
    var grid = cssVar("--grid", "#e1e0d9");
    var muted = cssVar("--muted", "#898781");
    var valueAxis = {
      grid: { color: grid, drawTicks: false },
      border: { color: cssVar("--baseline", "#c3c2b7") },
      ticks: { color: muted, callback: function (v) { return money(v); } },
      beginAtZero: true,
    };
    var categoryAxis = {
      grid: { display: false },
      border: { color: cssVar("--baseline", "#c3c2b7") },
      ticks: { color: muted, autoSkip: true, maxRotation: 0 },
    };
    return {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      indexAxis: horizontal ? "y" : "x",
      plugins: {
        legend: { display: false }, // single series: the card title names it
        tooltip: {
          callbacks: {
            label: function (ctx) {
              var v = horizontal ? ctx.parsed.x : ctx.parsed.y;
              return money(v);
            },
            footer: function (items) {
              var i = items[0].dataIndex;
              var unpriced = items[0].dataset.unpriced;
              if (unpriced && unpriced[i] > 0) {
                return unpriced[i] + " unpriced call(s) excluded";
              }
              return "";
            },
          },
        },
      },
      scales: horizontal
        ? { x: valueAxis, y: categoryAxis }
        : { x: categoryAxis, y: valueAxis },
    };
  }

  function buildChart(canvas, payload) {
    var series = cssVar("--series-1", "#2a78d6");
    var kind = canvas.dataset.chart || "hbar";
    var horizontal = kind === "hbar";
    var config;
    if (kind === "line") {
      config = {
        type: "line",
        data: {
          labels: payload.labels,
          datasets: [{
            data: payload.values,
            unpriced: payload.unpriced,
            borderColor: series,
            borderWidth: 2,
            pointRadius: 0,
            pointHoverRadius: 5,
            pointHoverBackgroundColor: series,
            tension: 0.2,
          }],
        },
        options: baseOptions(false),
      };
      config.options.interaction = { mode: "index", intersect: false };
    } else {
      config = {
        type: "bar",
        data: {
          labels: payload.labels,
          datasets: [{
            data: payload.values,
            unpriced: payload.unpriced,
            backgroundColor: series,
            borderRadius: 4, // rounded data end, square base
            maxBarThickness: 22,
          }],
        },
        options: baseOptions(horizontal),
      };
    }
    if (registry[canvas.id]) {
      registry[canvas.id].destroy();
    }
    registry[canvas.id] = new Chart(canvas, config);
  }

  function initCharts(root) {
    var canvases = (root || document).querySelectorAll("canvas[data-endpoint]");
    canvases.forEach(function (canvas) {
      fetch(canvas.dataset.endpoint)
        .then(function (r) { return r.json(); })
        .then(function (payload) { buildChart(canvas, payload); })
        .catch(function (err) { console.error("chart fetch failed", err); });
    });
  }

  document.addEventListener("DOMContentLoaded", function () { initCharts(document); });
  document.body.addEventListener("htmx:afterSwap", function (e) { initCharts(e.target); });
})();
