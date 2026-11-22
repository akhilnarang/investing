// Rewrites <time data-local-time> into the visitor's own time zone, keeping the server's text as the tooltip.
document.querySelectorAll("time[data-local-time]").forEach((time) => {
  const date = new Date(time.dateTime);
  if (Number.isNaN(date.getTime())) return;
  time.title = time.textContent.trim();
  time.textContent = new Intl.DateTimeFormat(undefined, {
    year: "numeric", month: "short", day: "2-digit",
    hour: "2-digit", minute: "2-digit", timeZoneName: "short",
  }).format(date);
});
