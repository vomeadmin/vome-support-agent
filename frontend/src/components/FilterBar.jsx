import { useState } from "react";

const FILTERS = [
  { key: "all", label: "All active" },
  { key: "p1", label: "P1 only" },
  { key: "bugs", label: "Bugs" },
  { key: "needs_review", label: "Needs review" },
  { key: "waiting", label: "Waiting on client" },
  { key: "final_review", label: "Final review" },
  { key: "resolved", label: "Resolved today" },
  { key: "unassigned", label: "Unassigned" },
];

export default function FilterBar({ activeFilter, onFilterChange, stats }) {
  return (
    <div className="filter-bar">
      <div className="stats-row">
        <div className="stat">
          <span className="stat-num">{stats.need_response || 0}</span>
          <span className="stat-label">need response</span>
        </div>
        <div className="stat">
          <span className="stat-num">{stats.needs_review || 0}</span>
          <span className="stat-label">needs review</span>
        </div>
        <div className="stat">
          <span className="stat-num">{stats.waiting_on_client || 0}</span>
          <span className="stat-label">waiting on client</span>
        </div>
        <div className="stat">
          <span className="stat-num">{stats.resolved_today || 0}</span>
          <span className="stat-label">resolved today</span>
        </div>
      </div>
      <div className="filter-tabs">
        {FILTERS.map((f) => (
          <button
            key={f.key}
            className={`filter-tab ${activeFilter === f.key ? "active" : ""}`}
            onClick={() => onFilterChange(f.key)}
          >
            {f.label}
          </button>
        ))}
      </div>
    </div>
  );
}
