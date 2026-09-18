import { useEffect, useState } from "react";
import { BrainCircuit, LoaderCircle, Pin, WifiOff } from "lucide-react";
import { formatTime } from "../../lib/formatters";
import {
  listProfileMemories,
  memoriesForAnswer,
  memoryCategoryLabels,
  memoryScopeLabels,
} from "../../lib/profile";
import { Drawer } from "../ui/Overlays";


const providerLabels = {
  local: "本地提取",
  deepseek: "模型提取",
  model: "模型提取",
  openai: "OpenAI 提取",
  qwen: "通义千问提取",
  moonshot: "Moonshot 提取",
  siliconflow: "SiliconFlow 提取",
  ollama: "Ollama 提取",
  custom: "模型提取",
  manual: "手动添加",
};


export function PersonalizationDrawer({ open, onClose, personalization, refreshToken = 0 }) {
  const [memories, setMemories] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const ids = personalization?.memory_ids || [];
  const idsKey = ids.join(",");

  useEffect(() => {
    if (!open || !ids.length) {
      if (!open) { setMemories([]); setError(""); }
      return undefined;
    }
    let current = true;
    setLoading(true);
    setError("");
    listProfileMemories({ ids })
      .then((payload) => {
        if (current) setMemories(memoriesForAnswer(payload?.items || [], personalization));
      })
      .catch((loadError) => {
        if (current) {
          setMemories(memoriesForAnswer([], personalization));
          setError(loadError.message || "无法读取本次记忆");
        }
      })
      .finally(() => { if (current) setLoading(false); });
    return () => { current = false; };
  }, [open, idsKey, refreshToken]);

  return (
    <Drawer open={open} onClose={onClose} eyebrow="PERSONALIZATION" title="本次个性化" label="本次个性化" className="personalization-drawer" dataUi="personalization-drawer">
      {loading ? <div className="memory-loading" role="status"><LoaderCircle size={17} /><span>正在读取</span></div> : <>
        <div className="personalization-facts">
          <span><BrainCircuit size={15} />{ids.length} 条记忆</span>
          <span>全局 {personalization?.global_count || 0}</span>
          <span>知识库 {personalization?.scoped_count || 0}</span>
          <span>{personalization?.sent_to_model ? "已发送模型" : "仅本地使用"}</span>
          {personalization?.retrieval_query_enriched && <span>检索已扩展</span>}
        </div>
        {error && <div className="error-banner personalization-error" role="alert"><WifiOff size={17} /><span>{error}</span></div>}
        <div className="personalization-memory-list" data-ui="personalization-memory-list">
          {memories.map((memory, index) => (
            <article className={memory.missing ? "missing" : ""} key={memory.id}>
              <header><span>{String(index + 1).padStart(2, "0")}</span>{memory.pinned && <Pin size={14} aria-label="已固定" />}</header>
              <p>{memory.display_text || memory.value}</p>
              {!memory.missing && <div>
                <span>{memoryScopeLabels[memory.scope] || memory.scope}</span>
                <span>{memoryCategoryLabels[memory.category] || memory.category}</span>
                <span>{providerLabels[memory.extraction_provider] || memory.extraction_provider}</span>
                {memory.updated_at && <span>{formatTime(memory.updated_at)}</span>}
              </div>}
            </article>
          ))}
          {!memories.length && !loading && <p className="memory-empty">本次没有使用长期记忆</p>}
        </div>
      </>}
    </Drawer>
  );
}
