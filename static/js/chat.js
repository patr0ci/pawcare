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
  text.classList.add("typing");
  input.value = "";
  input.disabled = true;

  let sources = [];
  try {
    const response = await fetch(form.action, { method: "POST", body: payload });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      text.textContent = data.error || "Something went wrong.";
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
        if (event.type === "delta") { text.textContent += event.text; reply.scrollIntoView({ block: "end" }); }
        if (event.type === "error") text.textContent = event.message;
        if (event.type === "done") {
          renderSources(reply, sources);
          const meta = document.createElement("div");
          meta.className = "meta";
          meta.textContent = `$${event.cost_usd.toFixed(5)}`;
          reply.append(meta);
        }
      }
    }
  } catch {
    text.textContent = "Connection lost. Please try again.";
  } finally {
    text.classList.remove("typing");
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
