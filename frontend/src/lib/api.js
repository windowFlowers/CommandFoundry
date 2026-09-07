export function apiBaseUrl() {
  return window.aegisDesktop?.apiBaseUrl || import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8002";
}

export async function fetchJson(path, { method = "GET", body } = {}) {
  const response = await fetch(`${apiBaseUrl()}${path}`, {
    method,
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    const raw = await response.text();
    let message = raw || `请求失败：${response.status}`;
    try {
      message = JSON.parse(raw).detail || message;
    } catch {
      // The backend may intentionally return plain text for fatal startup errors.
    }
    throw new Error(message);
  }
  return response.status === 204 ? null : response.json();
}

export function parseSseFrame(frame) {
  let event = "message";
  const data = [];
  for (const line of frame.split(/\r?\n/)) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
  }
  if (!data.length) return null;
  return { event, data: JSON.parse(data.join("\n")) };
}

export async function streamChat({ query, conversationId, onEvent }) {
  const response = await fetch(`${apiBaseUrl()}/chat/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query, conversation_id: conversationId || null }),
  });
  if (!response.ok || !response.body) throw new Error(`流式请求失败：${response.status}`);
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    const frames = buffer.split(/\r?\n\r?\n/);
    buffer = frames.pop() || "";
    for (const frame of frames) {
      const parsed = parseSseFrame(frame);
      if (parsed) onEvent?.(parsed);
    }
    if (done) break;
  }
  if (buffer.trim()) {
    const parsed = parseSseFrame(buffer);
    if (parsed) onEvent?.(parsed);
  }
}
