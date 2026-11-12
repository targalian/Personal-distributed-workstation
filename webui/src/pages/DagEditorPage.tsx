import { useEffect, useRef } from "react";

/** Shared editor keeps task fields, permissions and unsaved-change behavior consistent. */
export default function DagEditorPage({ taskId }: { taskId: string }) {
  const frame = useRef<HTMLIFrameElement>(null);
  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      if (event.origin !== location.origin || event.source !== frame.current?.contentWindow) return;
      if (event.data?.type === "dag-discuss") {
        if (!canLeaveDag()) return;
        location.href = "/?" + new URLSearchParams({ dag_task: event.data.task_id });
      }
      if (event.data?.type === "dag-expand") {
        frame.current?.classList.toggle("dag-expanded");
        frame.current?.contentWindow?.postMessage({type:"dag-expanded", expanded:frame.current.classList.contains("dag-expanded")}, location.origin);
      }
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, []);
  return <iframe ref={frame} id="spa-dag-frame" title="任务流程工作台"
    src={"/static/dag/index.html?" + new URLSearchParams({task_id: taskId})}
    style={{width:"100%",height:"calc(100vh - 110px)",minHeight:700,border:0}} />;
}

export function canLeaveDag(): boolean {
  const frame = document.getElementById("spa-dag-frame") as HTMLIFrameElement | null;
  const child = frame?.contentWindow as (Window & { DagWorkbench?: { canLeave(): boolean } }) | null;
  return child?.DagWorkbench?.canLeave() !== false;
}
