import { useEffect, useState } from "react";
import { BrainCircuit, LoaderCircle, WifiOff } from "lucide-react";
import { formatTime } from "../../lib/formatters";
import { Drawer } from "../ui/Overlays";
import { SelectMenu } from "../ui/SelectMenu";

const modelProviders = [
  { value: "deepseek", label: "DeepSeek", model: "deepseek-chat", baseUrl: "https://api.deepseek.com/v1", requiresKey: true },
  { value: "openai", label: "OpenAI", model: "gpt-4o-mini", baseUrl: "https://api.openai.com/v1", requiresKey: true },
  { value: "qwen", label: "通义千问 / DashScope", model: "qwen-plus", baseUrl: "https://dashscope.aliyuncs.com/compatible-mode/v1", requiresKey: true },
  { value: "moonshot", label: "Moonshot / Kimi", model: "moonshot-v1-8k", baseUrl: "https://api.moonshot.cn/v1", requiresKey: true },
  { value: "siliconflow", label: "SiliconFlow", model: "deepseek-ai/DeepSeek-V3", baseUrl: "https://api.siliconflow.cn/v1", requiresKey: true },
  { value: "ollama", label: "Ollama（本机）", model: "llama3.2", baseUrl: "http://127.0.0.1:11434/v1", requiresKey: false },
  { value: "custom", label: "自定义 OpenAI 兼容 API", model: "", baseUrl: "", requiresKey: true },
];

function providerDefaults(provider) {
  return modelProviders.find((item) => item.value === provider) || modelProviders.at(-1);
}

function providerDisplay(modelStatus) {
  return modelStatus?.providerLabel || providerDefaults(modelStatus?.provider).label;
}

export function SettingsDrawer({ open, onClose, modelStatus, onChanged }) {
  const [key, setKey] = useState("");
  const [provider, setProvider] = useState("deepseek");
  const [model, setModel] = useState("deepseek-chat");
  const [baseUrl, setBaseUrl] = useState("https://api.deepseek.com/v1");
  const [message, setMessage] = useState("");
  const [working, setWorking] = useState(false);
  const desktopApi = window.aegisDesktop?.modelConfig;
  useEffect(() => {
    if (!open) return;
    const defaults = providerDefaults(modelStatus?.provider || "deepseek");
    setProvider(modelStatus?.provider || defaults.value);
    setModel(modelStatus?.model || defaults.model);
    setBaseUrl(modelStatus?.baseUrl || defaults.baseUrl);
    setKey("");
    setMessage("");
  }, [open, modelStatus?.provider, modelStatus?.model, modelStatus?.baseUrl]);

  function changeProvider(nextProvider) {
    const defaults = providerDefaults(nextProvider);
    // API keys are provider-scoped and are never loaded into the renderer.
    // Clear an in-progress key when switching endpoints to avoid accidental
    // cross-provider submission.
    setKey("");
    setProvider(nextProvider);
    setModel(defaults.model);
    setBaseUrl(defaults.baseUrl);
    setMessage("");
  }

  async function run(action) {
    if (!desktopApi) { setMessage("模型配置仅在桌面端可用"); return; }
    setWorking(true); setMessage("");
    try {
      const currentProvider = providerDefaults(provider);
      const hasSavedKeyForProvider = Boolean(modelStatus?.configured && modelStatus?.provider === provider);
      const payload = {
        apiKey: key.trim() || undefined,
        provider,
        model: model.trim(),
        baseUrl: baseUrl.trim(),
      };
      if (action === "save") {
        if (currentProvider.requiresKey && !key.trim() && !hasSavedKeyForProvider) throw new Error("请输入 API Key");
        await desktopApi.save(payload); setKey(""); setMessage("模型配置已加密保存");
      } else if (action === "test") {
        const result = await desktopApi.test(payload); setMessage(result.message || "连接成功");
      } else {
        await desktopApi.clear(); setKey(""); setMessage("已清除密钥");
      }
      await onChanged();
    } catch (error) { setMessage(error.message || "操作失败"); }
    finally { setWorking(false); }
  }

  return (
    <Drawer open={open} onClose={onClose} eyebrow="MODEL SETTINGS" title="模型配置" label="设置" dataUi="settings-drawer">
      <div className="model-state compact-state"><span className={`status-dot ${modelStatus?.configured ? "ready" : ""}`} aria-hidden="true" /><strong>{modelStatus?.configured ? `${providerDisplay(modelStatus)} · ${modelStatus.model || model}` : "当前使用本地回答"}</strong></div>
      <label className="field-label" htmlFor="model-provider">模型提供商</label>
      <SelectMenu id="model-provider" value={provider} onChange={changeProvider} ariaLabel="模型提供商" options={modelProviders.map(({ value, label }) => ({ value, label }))} dataUi="model-provider-select" />
      <label className="field-label" htmlFor="model-name">模型名称</label>
      <input id="model-name" className="text-input" type="text" value={model} onChange={(event) => setModel(event.target.value)} placeholder="模型名称" autoComplete="off" />
      <label className="field-label" htmlFor="model-base-url">API Base URL</label>
      <input id="model-base-url" className="text-input" type="url" value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} placeholder="https://api.example.com/v1" autoComplete="off" />
      <label className="field-label" htmlFor="api-key">API Key</label>
      <input id="api-key" className="text-input" type="password" value={key} onChange={(event) => setKey(event.target.value)} placeholder={providerDefaults(provider).requiresKey ? "输入 API Key（不会写入 SQLite）" : "本机 Ollama 通常无需 API Key"} autoComplete="off" />
      <p className="settings-hint">支持 OpenAI 兼容的 /chat/completions 接口；Ollama 可直接连接本机服务。</p>
      {message && <p className="settings-message" role="status">{message}</p>}
      <div className="drawer-actions">
        <button className="secondary-button" type="button" disabled={working} onClick={() => run("test")}>测试真实对话</button>
        <button className="primary-button" type="button" disabled={working || !model.trim() || !baseUrl.trim() || (providerDefaults(provider).requiresKey && !key.trim() && !(modelStatus?.configured && modelStatus?.provider === provider))} onClick={() => run("save")}>{working ? "处理中" : "保存"}</button>
      </div>
      {modelStatus?.configured && <button className="clear-key" type="button" disabled={working} onClick={() => run("clear")}>清除已保存密钥</button>}
    </Drawer>
  );
}

const memorySectionLabels = {
  current_goal: "当前目标",
  environments: "运行环境",
  constraints: "约束",
  decisions: "已确认选择",
  open_questions: "待确认",
  history_topics: "历史主题",
};

export function MemoryDrawer({ open, onClose, memory, loading, error, onReset }) {
  const summary = memory?.summary || {};
  const sections = Object.entries(memorySectionLabels).filter(([key]) => {
    const value = summary[key];
    return Array.isArray(value) ? value.length > 0 : Boolean(value);
  });
  const statusText = memory?.status === "summarizing" ? "摘要中" : memory?.status === "pending" ? "等待摘要" : memory?.summary_provider === "local" ? "本地摘要" : memory?.summary_provider ? "模型摘要" : "最近对话";

  return (
    <Drawer open={open} onClose={onClose} eyebrow="CONVERSATION CONTEXT" title="会话上下文" label="会话上下文" className="memory-drawer" dataUi="memory-drawer">
      {loading ? <div className="memory-loading" role="status"><LoaderCircle size={17} /><span>正在读取</span></div> : error ? <div className="error-banner" role="alert"><WifiOff size={17} /><span>{error}</span></div> : memory && <>
        <div className="memory-facts"><span><BrainCircuit size={15} />{statusText}</span><span>{memory.estimated_tokens || 0} token</span>{memory.summary_updated_at && <span>{formatTime(memory.summary_updated_at)}</span>}</div>
        {sections.length > 0 && <div className="memory-summary">{sections.map(([key, label], index) => {
          const values = Array.isArray(summary[key]) ? summary[key] : [summary[key]];
          return <section key={key}><h3><span>{String(index + 1).padStart(2, "0")}</span>{label}</h3><div>{values.map((value, valueIndex) => <span key={`${key}-${valueIndex}`}>{value}</span>)}</div></section>;
        })}</div>}
        <section className="memory-recent"><h3><span>{String(sections.length + 1).padStart(2, "0")}</span>本次使用</h3>{memory.recent_messages?.length ? <div>{memory.recent_messages.map((message) => <article key={message.id}><span>{message.role === "user" ? "你" : "AegisCopilot"}</span><p>{message.preview}</p></article>)}</div> : <div className="memory-empty">未使用历史消息</div>}</section>
        {memory.last_error && <div className="memory-error">模型摘要失败 · {memory.last_error}</div>}
        <button className="clear-memory" type="button" onClick={onReset} data-ui="clear-memory">清空上下文</button>
      </>}
    </Drawer>
  );
}
