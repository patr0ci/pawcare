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

function actionCard(action, waiting = false) {
  // A proposed write. Nothing happens on the server until the user clicks Confirm.
  const card = document.createElement("div");
  card.className = "action";
  card.innerHTML = `<div class="action-title">Please confirm</div><div class="action-summary"></div>
    <div class="action-buttons"><button class="button confirm">Confirm</button><button class="link dismiss">Not now</button></div>`;
  card.querySelector(".action-summary").textContent = action.summary;
  const buttons = card.querySelectorAll("button");
  // Proposed mid-turn: locked until the turn's reply is saved. Confirming earlier logged "Booked: ..." before
  // that reply, so after a reload the two showed in the wrong order.
  buttons.forEach((b) => (b.disabled = waiting));
  const note = (text) => {
    const el = document.createElement("div");
    el.className = "action-note small";
    el.textContent = text;
    card.querySelector(".action-summary").after(el);
  };
  const decide = async (verb) => {
    buttons.forEach((b) => (b.disabled = true));
    card.querySelector(".action-note")?.remove();
    let response;
    try {
      response = await fetch(`/assistant/actions/${action.id}/${verb}/`, {
        method: "POST",
        headers: { "X-CSRFToken": csrf() },
      });
    } catch {
      // Still pending as far as we know. A retry is safe: if it did go through, the server says so (409).
      buttons.forEach((b) => (b.disabled = false));
      note("Connection lost. Please try again.");
      return;
    }
    if (response.redirected) {
      // login_required sent the POST on to the login page: the demo account behind this page is gone.
      card.querySelector(".action-buttons").remove();
      note("Your session has ended. Reload the page to start a new demo.");
      return;
    }
    const data = await response.json().catch(() => ({ message: "Something went wrong." }));
    card.querySelector(".action-buttons").remove();
    card.querySelector(".action-title").textContent = data.status === "confirmed" ? "Done" : "No changes";
    card.classList.add(data.status || "failed");
    if (data.status === "confirmed") {
      // The point of the demo: the change landed in the clinic's own system, not just in the chat.
      const link = document.createElement("a");
      link.href = "/my-pets/";
      link.className = "small";
      link.textContent = "See it in My pets →";
      card.append(link);
    }
    // 409: settled earlier, from another tab or before Back. Nothing new was said, so no new chat message.
    if (response.status === 409) note(data.message);
    else bubble("assistant", data.message);
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
  let left;
  const setRemaining = (n) => {
    if (n === undefined) return;
    left = n;
    const counter = document.getElementById("remaining");
    if (counter) counter.textContent = n;
  };
  const proposed = []; // this turn's cards, unlocked when it ends
  try {
    const response = await fetch(form.action, { method: "POST", body: payload });
    const isStream = (response.headers.get("Content-Type") || "").startsWith("text/event-stream");
    if (!response.ok || !isStream) {
      const data = await response.json().catch(() => ({}));
      text.textContent =
        data.error ||
        (response.redirected ? "Your session has ended. Reload the page to start a new demo." : "Something went wrong.");
      reply.classList.add("error");
      if (response.status === 429) left = 0; // daily limit or budget: nothing more to send today
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
        if (event.type === "action") {
          const card = actionCard(event, true);
          // Next to the reply, not inside it: where a reload puts it (after this turn's earlier cards).
          (proposed.at(-1) || reply).after(card);
          proposed.push(card);
          card.scrollIntoView({ block: "end" });
        }
        if (event.type === "error") {
          text.textContent = event.message;
          reply.classList.add("error");
          setRemaining(event.remaining); // the question may have been counted before it failed
        }
        if (event.type === "done") {
          renderSources(reply, sources.filter((s) => event.cited.includes(s.number)));
          const meta = document.createElement("div");
          meta.className = "meta";
          // Same line as a reloaded message: which model answered, and what it cost in tokens, money and time.
          meta.textContent = `${event.model} · ${event.tokens} tokens · $${event.cost_usd.toFixed(5)} · ${event.latency_ms} ms`;
          reply.append(meta);
          setRemaining(event.remaining);
        }
      }
    }
  } catch {
    text.textContent = "Connection lost. Please try again.";
    reply.classList.add("error");
  } finally {
    text.classList.remove("typing");
    status.remove();
    const outOfMessages = left === 0;
    input.disabled = outOfMessages;
    form.querySelector("button[type=submit]").disabled = outOfMessages;
    if (!outOfMessages) input.focus();
    // After done or error the reply is saved; a stream that just dropped has nothing left to wait for either.
    proposed.forEach((card) => card.querySelectorAll("button").forEach((b) => (b.disabled = false)));
  }
}

form.addEventListener("submit", (e) => {
  e.preventDefault();
  const question = input.value.trim();
  if (question) send(question);
});

log.addEventListener("click", (e) => {
  if (e.target.classList.contains("chip") && !input.disabled) send(e.target.textContent);
});
