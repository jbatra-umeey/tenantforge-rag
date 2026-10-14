"use strict";
const $ = id => document.getElementById(id);
let token = "", conversation = null, poll = null, offset = 0;
function notice(text, error = false) { $("notice").textContent = text; $("notice").className = "notice" + (error ? " error" : ""); }
async function api(path, options = {}) {
  if (!token) throw new Error("Connect with your workspace token first.");
  const result = await fetch(path, {...options, headers: {...options.headers, Authorization: `Bearer ${token}`}});
  const data = await result.json();
  if (!result.ok) throw new Error(typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail));
  return data;
}
function node(tag, text, cls) { const n = document.createElement(tag); if (text) n.textContent = text; if (cls) n.className = cls; return n; }
function resetChat() { conversation = null; $("chat").replaceChildren(node("p", "New conversation. Ask a question about your sources.", "muted")); }
async function refresh(append = false) {
  if (!append) offset = 0;
  const [stats, docs] = await Promise.all([api("/v1/stats"), api(`/v1/documents?offset=${offset}&limit=50`)]);
  $("tenant").textContent = stats.tenant; $("identity").textContent = `${stats.user} · ${stats.role}`;
  $("mode").textContent = stats.embedding_backend === "hash" ? "Local · hash + extractive" : `${stats.embedding_backend} · ${stats.generation_backend}`;
  $("ready-count").textContent = stats.documents.ready || 0;
  $("queued-count").textContent = (stats.documents.queued || 0) + (stats.documents.processing || 0);
  $("budget-count").textContent = new Intl.NumberFormat().format(stats.daily_reserved_token_units);
  if (!append) $("documents").replaceChildren();
  if (!docs.items.length && !append) $("documents").append(node("p", "No documents yet. Add your first source above.", "muted"));
  for (const doc of docs.items) {
    const row = node("div", null, "doc"), body = node("div", null, "doc-text");
    body.append(node("div", doc.name, "doc-title"), node("div", `${doc.chunk_count} chunks · ${Math.ceil(doc.size / 1024)} KB`, "doc-info"));
    if (doc.error) body.append(node("div", doc.error, "doc-info"));
    row.append(body, node("span", doc.status, `status ${doc.status}`));
    if (stats.role !== "reader") {
      if (doc.status === "failed") {
        const retry = node("button", "Retry", "quiet");
        retry.onclick = async () => { try { await api(`/v1/documents/${doc.id}/retry`, {method: "POST"}); await refresh(); } catch(e) {notice(e.message, true);} };
        row.append(retry);
      }
      const remove = node("button", "×", "quiet"); remove.setAttribute("aria-label", `Delete ${doc.name}`);
      remove.onclick = async () => { if (!confirm(`Delete ${doc.name} and its searchable chunks?`)) return;
        try { await api(`/v1/documents/${doc.id}`, {method: "DELETE"}); await refresh(); notice("Source deleted. Future queries use the updated corpus."); } catch(e) { notice(e.message, true); } };
      row.append(remove);
    }
    $("documents").append(row);
  }
  offset += docs.items.length; $("load-more").classList.toggle("hidden", docs.items.length < 50);
}
$("connect").onclick = async () => {
  clearInterval(poll); token = $("token").value.trim().replace(/^Bearer\s+/i, ""); resetChat();
  try { await refresh(); $("token").value = ""; notice("Connected. Upload a source or ask a question about a ready document."); poll = setInterval(() => refresh().catch(() => {}), 6000); }
  catch(e) {token = ""; notice(e.message, true);}
};
$("refresh").onclick = () => refresh().catch(e => notice(e.message, true));
$("load-more").onclick = () => refresh(true).catch(e => notice(e.message, true));
$("new-chat").onclick = resetChat;
$("example").onclick = () => { $("question").value = "What is the annual leave policy?"; $("question").focus(); };
$("upload-form").onsubmit = async event => {
  event.preventDefault(); $("upload-button").disabled = true;
  try { JSON.parse($("metadata").value); const form = new FormData(); form.append("file", $("file").files[0]); form.append("metadata", $("metadata").value);
    const result = await api("/v1/documents", {method:"POST", body:form}); notice(result.deduplicated ? "This source already exists in your workspace." : "Upload accepted. The worker will parse and index your source."); await refresh(); }
  catch(e) { notice(e.message, true); } finally {$("upload-button").disabled = false;}
};
$("question-form").onsubmit = async event => {
  event.preventDefault(); const question = $("question").value.trim(); if (!question) return;
  $("ask-button").disabled = true;
  if ($("chat").querySelector(".empty")) $("chat").replaceChildren();
  const user = node("div", question, "message user"); $("chat").append(user);
  try {
    const filters = {}; if ($("filter-key").value.trim()) filters[$("filter-key").value.trim()] = $("filter-value").value.trim();
    const result = await api("/v1/query", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({question, conversation_id:conversation, filters, quality:$("quality").value})});
    conversation = result.conversation_id;
    const answer = node("div", null, "message"); answer.append(node("span", "TENANTFORGE", "label"), node("div", result.answer));
    const sources = node("div", null, "sources");
    for (const source of result.citations) { const button = node("button", `[${source.number}] ${source.name} · ${source.locator}`);
      button.onclick = async () => { try { const chunk = await api(source.url); $("source-title").textContent = chunk.name; $("source-location").textContent = chunk.locator; $("source-content").textContent = chunk.content; $("source-dialog").showModal(); } catch(e) {notice(e.message, true);} }; sources.append(button); }
    answer.append(sources, node("small", `${result.model} · ${result.latency_ms} ms · ${result.cache_hit ? "cached answer" : "fresh retrieval"}`));
    $("chat").append(answer); $("question").value = ""; $("chat").scrollTop = $("chat").scrollHeight; await refresh();
  } catch(e) { notice(e.message, true); user.append(node("small", " — request failed")); }
  finally { $("ask-button").disabled = false; }
};
$("close-dialog").onclick = () => $("source-dialog").close();
