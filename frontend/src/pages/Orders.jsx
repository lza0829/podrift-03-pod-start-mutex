import React, { useEffect, useState } from "react";
import { api } from "../api";

export default function Orders() {
  const [pods, setPods] = useState([]);
  const [orders, setOrders] = useState([]);
  const [receipt, setReceipt] = useState(null);
  const load = () =>
    Promise.all([api.pods(), api.orders()]).then(([p, o]) => {
      setPods(p);
      setOrders(o);
    });
  useEffect(() => { load(); }, []);
  const act = (podId, action) =>
    api.createOrder({ pod_id: podId, action }).then((r) => { setReceipt(r); return load(); });
  const close = (orderId, outcome) =>
    api.closeOrder(orderId, { outcome }).then((r) => { setReceipt(r); return load(); });

  const openOrders = orders.filter((o) => o.status === "启动中");

  return (
    <>
      <div className="hero">
        <h1>启停单</h1>
        <p>
          互斥口径：<strong>并入延占</strong>。前单未收口时再开启动，第二张并入前单并延长占用，
          不新开运行席、不装成独立完成。
        </p>
      </div>

      <div className="panel">
        <div className="row-actions">
          {pods.map((p) => (
            <span key={p.id}>
              <strong>{p.name}</strong>{" "}
              <span className={`tag ${p.run_state === "运行中" ? "ok" : p.run_state === "启动中" ? "warn" : ""}`}>
                {p.run_state}
              </span>{" "}
              <button className="btn" onClick={() => act(p.id, "启动")}>启动</button>
              <button className="btn" onClick={() => act(p.id, "停止")}>停止</button>
            </span>
          ))}
        </div>
      </div>

      {receipt && (
        <div className="panel">
          <strong>第二张回执</strong>
          <pre className="receipt">{JSON.stringify(receipt, null, 2)}</pre>
        </div>
      )}

      {openOrders.length > 0 && (
        <div className="panel">
          <strong>未收口启停单 — 收口须走这里，不能对并启单直接收口</strong>
          <div className="row-actions">
            {openOrders.map((o) => (
              <span key={o.id}>
                单 #{o.id} · 舱 {o.pod_id} · 席 {o.seat_no} · 戳 {o.seat_stamp} · 并启 {o.merge_count} 张{" "}
                <button className="btn" onClick={() => close(o.id, "已完成")}>收口成功</button>
                <button className="btn" onClick={() => close(o.id, "失败")}>收口失败</button>
              </span>
            ))}
          </div>
        </div>
      )}

      <div className="panel">
        <table>
          <thead>
            <tr>
              <th>ID</th><th>舱</th><th>动作</th><th>状态</th><th>说明</th>
              <th>并入</th><th>并启</th><th>延占</th><th>席</th><th>占用戳</th><th>时间</th>
            </tr>
          </thead>
          <tbody>
            {orders.map((o) => (
              <tr key={o.id}>
                <td>{o.id}</td>
                <td>{o.pod_id}</td>
                <td>{o.action}</td>
                <td>
                  <span className={`tag ${o.status === "已完成" ? "ok" : o.status === "失败" ? "bad" : o.status === "启动中" ? "warn" : ""}`}>
                    {o.status}
                  </span>
                </td>
                <td>{o.note}</td>
                <td>{o.merged_into_id ? `#${o.merged_into_id}` : "—"}</td>
                <td>{o.coboot ? "并启" : "—"}</td>
                <td>{o.extend_sec ? `${o.extend_sec}s` : "—"}</td>
                <td>{o.seat_no ?? "—"}</td>
                <td className="mono">{o.seat_stamp || "—"}</td>
                <td>{o.created_at}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
