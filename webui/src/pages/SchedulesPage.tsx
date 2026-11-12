/** Shared schedule and authenticated IM management surface. */
export default function SchedulesPage() {
  return <iframe title="周期任务与消息入口" src="/static/schedules/index.html"
    style={{ width: '100%', height: 'calc(100vh - 110px)', border: 0, minHeight: 600 }} />;
}
