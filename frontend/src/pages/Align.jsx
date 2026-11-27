import React, { useEffect, useState } from "react";
import { api } from "../api";

export default function Align() {
  const [data, setData] = useState(null);
  const load = () => api.align().then(setData);
  useEffect(() => { load(); }, []);
  if (!data) return null;
  const b = data["双席桶"];
  return (
    <>
      <div className="hero">
        <h1>对读屏</h1>
        <p>
          互斥口径：<strong>{data["互斥口径"]}</strong>。双席桶按舱计占用中运行席（硬上限 1），
          并启单只算「并入」不算成功席。「放行再开」与「再开方式」由订单/运行席真源实时重放得出。
        </p>
      </div>

      <div className="stat-strip">
        <div className="stat"><div className="k">双席桶合计</div><div className="v">{b["双席桶合计"]}</div></div>
        <div className="stat"><div className="k">已成功席合计</div><div className="v">{b["已成功席合计"]}</div></div>
        <div className="stat"><div className="k">并启单数</div><div className="v">{b["并启单数"]}</div></div>
        <div className="stat"><div className="k">未收口舱 / 脏舱</div><div className="v">{b["未收口舱数"]} / {b["脏舱数"]}</div></div>
      </div>

      <div className="panel">
        <table>
          <thead>
            <tr>
              <th>舱</th><th>运行</th><th>探针</th><th>镜像</th>
              <th>未收口单</th><th>占用中运行席</th><th>并启延占</th>
              <th>双席桶</th><th>已成功席</th><th>放行再开</th><th>再开方式</th><th>脏项</th>
            </tr>
          </thead>
          <tbody>
            {data["舱体投影"].map((r) => (
              <tr key={r.id}>
                <td>{r.name}</td>
                <td><span className={`tag ${r.run_state === "运行中" ? "ok" : r.run_state === "启动中" ? "warn" : ""}`}>{r.run_state}</span></td>
                <td><span className={`tag ${r.probe_color === "绿" ? "ok" : "bad"}`}>{r.probe_color} · {r.fail_streak}</span></td>
                <td>{r.pull_state}</td>
                <td className="mono">
                  {r["未收口单"].length
                    ? r["未收口单"].map((o) => `#${o.id} 席${o.seat_no} ×${o.merge_count}`).join("，")
                    : "—"}
                </td>
                <td className="mono">
                  {r["占用中运行席"].length
                    ? r["占用中运行席"].map((s) => `${s.stamp} 至${s.expire_at} 延占${s.extended_sec}s`).join("，")
                    : "—"}
                </td>
                <td className="mono">
                  {r["并启单"].length
                    ? r["并启单"].map((m) => `#${m.id}→#${m.merged_into_id} +${m.extend_sec}s`).join("，")
                    : "—"}
                </td>
                <td>{r["双席桶"]}</td>
                <td>{r["已成功席"]}</td>
                <td>
                  <span className={`tag ${r["放行再开"] ? "ok" : "bad"}`}>{r["放行再开"] ? "可" : "否"}</span>
                </td>
                <td className="mono">{r["再开方式"]}</td>
                <td className="mono">{r["脏项"].length ? r["脏项"].join("；") : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
