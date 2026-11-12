/** Shared source-linked memory management surface. */
export default function MemoriesPage() {
  return <iframe title="记忆与历史" src="/static/memories/index.html"
    style={{ width: '100%', height: 'calc(100vh - 110px)', border: 0, minHeight: 600 }} />;
}
