// Streams the assistant's answer over SSE (fetch + ReadableStream; EventSource can't POST).
const form = document.getElementById("composer");
const input = document.getElementById("message");
const log = document.getElementById("log");

function bubble(role, text = "") {
  log.querySelector(".empty")?.remove();
  const el = document.createElement("div");
  el.className = `bubble ${role}`;
  const body = document.createElement("div");
  body.className = "text";
  body.textContent = text;
  el.append(body);
  log.append(el);
  el.scrollIntoView({ block: "end" });
  return el;
}

const csrf = () => form.querySelector("[name=csrfmiddlewaretoken]").value;

function actionCard(action) {
  // A proposed write. Nothing happens on the server until the user clicks Confirm.
  const card = document.createElement("div");
  card.className = "action";
  card.innerHTML = `<div class="action-title">Please confirm</div><div class="action-summary"></div>
    <div class="action-buttons"><button class="button confirm">Confirm</button><button class="link dismiss">Not now</button></div>`;
  card.querySelector(".action-summary").textContent = action.summary;
  const decide = async (verb) => {
    card.querySelectorAll("button").forEach((b) => (b.disabled = true));
    const response = await fetch(`/assistant/actions/${action.id}/${verb}/`, {
      method: "POST",
      headers: { "X-CSRFToken": csrf() },
    });
    const data = await response.json().catch(() => ({ message: "Something went wrong." }));
    card.querySelector(".action-buttons").remove();
    card.querySelector(".action-title").textContent = data.status === "confirmed" ? "Done" : "No changes";
    card.classList.add(data.status || "failed");
    bubble("assistant", data.message);
  };
  card.querySelector(".confirm").addEventListener("click", () => decide("confirm"));
  card.querySelector(".dismiss").addEventListener("click", () => decide("dismiss"));
  return card;
}

document.querySelectorAll("[data-action]").forEach((el) => el.replaceWith(actionCard(JSON.parse(el.dataset.action))));

function renderSources(el, sources) {
  if (!sources.length) return;
  const box = document.createElement("div");
  box.className = "sources";
  for (const s of sources) {
    const a = document.createElement("a");
    a.href = s.url;
    a.target = "_blank";
    a.textContent = `[${s.number}] ${s.title}`;
    box.append(a);
  }
  el.append(box);
}

async function send(question) {
  const payload = new FormData(form);
  payload.set("message", question);
  bubble("user", question);
  const reply = bubble("assistant");
  const text = reply.querySelector(".text");
  const status = document.createElement("div");
  status.className = "status";
  reply.prepend(status);
  text.classList.add("typing");
  input.value = "";
  input.disabled = true;

  let sources = [];
  try {
    const response = await fetch(form.action, { method: "POST", body: payload });
    const isStream = (response.headers.get("Content-Type") || "").startsWith("text/event-stream");
    if (!response.ok || !isStream) {
      const data = await response.json().catch(() => ({}));
      text.textContent =
        data.error ||
        (response.redirected ? "Your session has ended. Reload the page to start a new demo." : "Something went wrong.");
      return;
    }
    const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += value;
      const events = buffer.split("\n\n");
      buffer = events.pop();
      for (const raw of events) {
        if (!raw.startsWith("data: ")) continue;
        const event = JSON.parse(raw.slice(6));
        if (event.type === "sources") sources = event.sources;
        if (event.type === "delta") { status.textContent = ""; text.textContent += event.text; reply.scrollIntoView({ block: "end" }); }
        if (event.type === "tool") status.textContent = `${event.label}…`;
        if (event.type === "action") { reply.append(actionCard(event)); reply.scrollIntoView({ block: "end" }); }
        if (event.type === "error") text.textContent = event.message;
        if (event.type === "done") {
          renderSources(reply, sources.filter((s) => event.cited.includes(s.number)));
          const meta = document.createElement("div");
          meta.className = "meta";
          meta.textContent = `$${event.cost_usd.toFixed(5)}`;
          reply.append(meta);
          const left = document.getElementById("remaining");
          if (left && event.remaining !== undefined) left.textContent = event.remaining;
        }
      }
    }
  } catch {
    text.textContent = "Connection lost. Please try again.";
  } finally {
    text.classList.remove("typing");
    status.remove();
    input.disabled = false;
    input.focus();
  }
}

form.addEventListener("submit", (e) => {
  e.preventDefault();
  const question = input.value.trim();
  if (question) send(question);
});

log.addEventListener("click", (e) => {
  if (e.target.classList.contains("chip")) send(e.target.textContent);
});
