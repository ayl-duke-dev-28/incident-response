(() => {
  const root = document.querySelector("[data-incident-id]");
  if (!root || !window.EventSource) return;

  const incidentId = root.dataset.incidentId;
  let currentVersion = root.dataset.incidentVersion;
  const stream = new EventSource(`/events/incidents/${encodeURIComponent(incidentId)}`);

  stream.addEventListener("incident", async (event) => {
    if (!event.lastEventId || event.lastEventId === currentVersion) return;
    const response = await fetch(window.location.href, {
      credentials: "same-origin",
      headers: { Accept: "text/html" },
    });
    if (!response.ok) return;
    const next = new DOMParser().parseFromString(await response.text(), "text/html");
    document.title = next.title;
    document.body.innerHTML = next.body.innerHTML;
    currentVersion = event.lastEventId;
  });
})();
