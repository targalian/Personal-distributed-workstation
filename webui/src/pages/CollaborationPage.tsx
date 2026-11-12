/** Share the authenticated collaboration workbench with the classic dashboard. */
export default function CollaborationPage() {
  return <iframe title="协作工作台" src="/static/collaboration/index.html"
    style={{ width: '100%', height: 'calc(100vh - 110px)', border: 0, minHeight: 600 }} />;
}
