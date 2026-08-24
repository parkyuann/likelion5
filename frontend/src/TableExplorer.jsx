import { useEffect, useRef, useState, useCallback } from "react";
import {
  searchTables,
  listFavorites,
  addFavorite,
  removeFavorite,
} from "./api.js";
import { StarIcon, ChartIcon, LinkIcon } from "./icons.jsx";
import "./TableExplorer.css";

function formatLatestPeriod(value) {
  if (value == null || value === "") return "—";
  const digits = String(value).replace(/\D/g, "");
  if (digits.length === 8) {
    return `${digits.slice(0, 4)}년 ${digits.slice(4, 6)}월 ${digits.slice(6, 8)}일`;
  }
  if (digits.length === 6) {
    return `${digits.slice(0, 4)}년 ${digits.slice(4, 6)}월`;
  }
  if (digits.length === 4) return `${digits}년`;
  return String(value);
}

function categoryParts(path) {
  return String(path || "")
    .split(">")
    .map((part) => part.trim())
    .filter(Boolean);
}

// 통계표 카드 한 줄
function TableRow({ table, favorited, onToggle, canFavorite, selected, onSelect }) {
  return (
    <li
      className={`tx-row ${selected ? "selected" : ""}`}
      onClick={() => onSelect(table)}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") onSelect(table);
      }}
      role="button"
      tabIndex={0}
      aria-pressed={selected}
    >
      <div className="tx-row-main">
        <span className="tx-row-title">{table.tbl_name}</span>
        {table.category_path && (
          <span className="tx-row-cat">{table.category_path}</span>
        )}
        <span className="tx-row-meta">
          {table.org_name}
          {table.latest_period
            ? ` · 기준 ${formatLatestPeriod(table.latest_period)}`
            : ""}
        </span>
      </div>
      <div className="tx-row-actions">
        <button
          className={`tx-star ${favorited ? "on" : ""}`}
          onClick={(e) => {
            e.stopPropagation();
            onToggle(table);
          }}
          title={
            canFavorite
              ? favorited
                ? "즐겨찾기 해제"
                : "즐겨찾기"
              : "로그인이 필요해요"
          }
          aria-pressed={favorited}
          aria-label="즐겨찾기"
        >
          <StarIcon fill={favorited ? "currentColor" : "none"} />
        </button>
        <a
          className="tx-link"
          href={table.kosis_url}
          target="_blank"
          rel="noreferrer"
          title="KOSIS에서 열기"
          aria-label="KOSIS에서 열기"
          onClick={(e) => e.stopPropagation()}
        >
          <LinkIcon />
        </a>
      </div>
    </li>
  );
}

function TableExplorer({ initialTab = "search", user, onClose, onRequireLogin }) {
  const [tab, setTab] = useState(initialTab);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState([]);
  const [total, setTotal] = useState(0);
  const [organizationFacets, setOrganizationFacets] = useState([]);
  const [searching, setSearching] = useState(false);
  const [favorites, setFavorites] = useState([]);
  const [favKeys, setFavKeys] = useState(() => new Set());
  const [orgFilter, setOrgFilter] = useState("all");
  const [selectedKey, setSelectedKey] = useState(null);
  const inputRef = useRef(null);
  const canFavorite = !!user?.backend;

  // Esc 닫기 + 검색탭 포커스
  useEffect(() => {
    const onKey = (e) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  useEffect(() => {
    if (tab === "search") inputRef.current?.focus();
  }, [tab]);

  // 검색(디바운스)
  useEffect(() => {
    if (tab !== "search") return;
    setSearching(true);
    const id = setTimeout(async () => {
      try {
        const r = await searchTables(query, {
          organization: orgFilter === "all" ? "" : orgFilter,
        });
        setResults(r.items || []);
        setTotal(r.total ?? (r.items || []).length);
        setOrganizationFacets(r.organizations || []);
        setFavKeys((prev) => {
          const next = new Set(prev);
          (r.items || []).forEach((it) => {
            if (it.favorited) next.add(it.table_key);
          });
          return next;
        });
      } catch {
        setResults([]);
      } finally {
        setSearching(false);
      }
    }, 300);
    return () => clearTimeout(id);
  }, [query, tab, orgFilter]);

  // 즐겨찾기 목록 로드
  const loadFavorites = useCallback(async () => {
    if (!canFavorite) return;
    try {
      const r = await listFavorites();
      setFavorites(r.items || []);
      setFavKeys(new Set((r.items || []).map((it) => it.table_key)));
    } catch {
      /* 무시 */
    }
  }, [canFavorite]);

  useEffect(() => {
    if (tab === "favorites") loadFavorites();
    setOrgFilter("all");
    setSelectedKey(null);
  }, [tab, loadFavorites]);

  async function toggleFavorite(table) {
    if (!canFavorite) {
      onRequireLogin?.();
      return;
    }
    const key = table.table_key;
    const isOn = favKeys.has(key);
    // 낙관적 업데이트
    setFavKeys((prev) => {
      const next = new Set(prev);
      if (isOn) next.delete(key);
      else next.add(key);
      return next;
    });
    try {
      if (isOn) {
        await removeFavorite(key);
        setFavorites((prev) => prev.filter((t) => t.table_key !== key));
      } else {
        await addFavorite(key);
        if (tab === "favorites") loadFavorites();
      }
    } catch {
      // 실패 시 롤백
      setFavKeys((prev) => {
        const next = new Set(prev);
        if (isOn) next.add(key);
        else next.delete(key);
        return next;
      });
    }
  }

  const list = tab === "search" ? results : favorites;
  const favoriteOrganizationCounts = favorites.reduce((acc, table) => {
    if (table.org_name) acc.set(table.org_name, (acc.get(table.org_name) || 0) + 1);
    return acc;
  }, new Map());
  const organizations =
    tab === "search"
      ? organizationFacets
      : [...favoriteOrganizationCounts.entries()]
          .map(([name, count]) => ({ name, count }))
          .sort((a, b) => a.name.localeCompare(b.name, "ko"));
  const filteredList =
    tab === "search" || orgFilter === "all"
      ? list
      : list.filter((table) => table.org_name === orgFilter);
  const selectedTable =
    filteredList.find((table) => table.table_key === selectedKey) || null;

  return (
    <div className="modal-overlay tx-overlay" onClick={onClose}>
      <div
        className="tx-modal"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label="통계표 탐색"
      >
        <button className="tx-close" onClick={onClose} aria-label="닫기">
          ✕
        </button>

        <div className="tx-head">
          <span className="tx-head-icon" aria-hidden="true">
            <ChartIcon size="1.4em" />
          </span>
          <h2 className="tx-title">통계표</h2>
        </div>

        <div className="tx-tabs" role="tablist">
          <button
            className={`tx-tab ${tab === "search" ? "active" : ""}`}
            onClick={() => setTab("search")}
            role="tab"
            aria-selected={tab === "search"}
          >
            탐색
          </button>
          <button
            className={`tx-tab ${tab === "favorites" ? "active" : ""}`}
            onClick={() => setTab("favorites")}
            role="tab"
            aria-selected={tab === "favorites"}
          >
            <StarIcon size="0.95em" /> 즐겨찾기
          </button>
        </div>

        {tab === "search" && (
          <div className="tx-search">
            <span className="tx-search-icon" aria-hidden="true">⌕</span>
            <input
              ref={inputRef}
              type="search"
              value={query}
              onChange={(e) => {
                setQuery(e.target.value);
                setOrgFilter("all");
              }}
              placeholder="통계표 이름으로 검색 (예: 실업률, 인구)"
            />
            {!searching && <span className="tx-result-count">{total.toLocaleString()}개</span>}
          </div>
        )}

        <div className="tx-org-compact">
          <label htmlFor="tx-org-select">작성 기관</label>
          <select
            id="tx-org-select"
            value={orgFilter}
            onChange={(e) => setOrgFilter(e.target.value)}
          >
            <option value="all">전체 기관</option>
            {organizations.map((org) => (
              <option key={org.name} value={org.name}>
                {org.name} ({org.count.toLocaleString()})
              </option>
            ))}
          </select>
        </div>

        <div className="tx-workspace">
          <aside className="tx-filter-panel" aria-label="통계표 필터">
            <span className="tx-filter-heading">작성 기관</span>
            <button
              className={`tx-filter ${orgFilter === "all" ? "active" : ""}`}
              onClick={() => setOrgFilter("all")}
            >
              <span>전체</span>
              <strong>{(tab === "search" ? total : list.length).toLocaleString("ko-KR")}</strong>
            </button>
            {organizations.map((org) => (
              <button
                key={org.name}
                className={`tx-filter ${orgFilter === org.name ? "active" : ""}`}
                onClick={() => setOrgFilter(org.name)}
              >
                <span>{org.name}</span>
                <strong>{org.count.toLocaleString()}</strong>
              </button>
            ))}
            <div className="tx-filter-note">
              전체 검색 결과를 기준으로 작성 기관을 표시합니다.
            </div>
          </aside>

          <div className="tx-body">
            {tab === "favorites" && !canFavorite ? (
              <div className="tx-empty">
                <p>로그인하면 관심 통계표를 즐겨찾기로 저장할 수 있어요.</p>
                <button className="tx-login" onClick={onRequireLogin}>
                  로그인
                </button>
              </div>
            ) : searching && tab === "search" ? (
              <div className="tx-empty tx-searching">
                <span className="tx-mini-grid" aria-hidden="true" />
                <p>관련 통계표를 찾고 있어요…</p>
              </div>
            ) : filteredList.length === 0 ? (
              <p className="tx-empty">
                {tab === "search"
                  ? "검색 결과가 없어요. 다른 키워드나 기관을 선택해 보세요."
                  : "아직 즐겨찾기한 통계표가 없어요. 탐색에서 ⭐를 눌러 추가해요."}
              </p>
            ) : (
              <ul className="tx-list">
                {filteredList.map((table) => (
                  <TableRow
                    key={table.table_key}
                    table={table}
                    favorited={favKeys.has(table.table_key)}
                    canFavorite={canFavorite}
                    onToggle={toggleFavorite}
                    selected={selectedKey === table.table_key}
                    onSelect={(next) => setSelectedKey(next.table_key)}
                  />
                ))}
              </ul>
            )}
          </div>

          <aside className={`tx-preview ${selectedTable ? "show" : ""}`} aria-live="polite">
            {selectedTable ? (
              <>
                <span className="tx-preview-label">선택한 통계표</span>
                <h3>{selectedTable.tbl_name}</h3>
                <div className="tx-preview-summary">
                  <span>통계표 안내</span>
                  <p>
                    <strong>{selectedTable.org_name || "작성 기관"}</strong>에서 제공하는
                    {selectedTable.category_path
                      ? ` ${categoryParts(selectedTable.category_path).at(-1)} 분야의 `
                      : " "}
                    <strong>{selectedTable.tbl_name}</strong> 자료입니다.
                  </p>
                </div>
                {selectedTable.category_path && (
                  <div className="tx-preview-category">
                    <span>분류 경로</span>
                    <div>
                      {categoryParts(selectedTable.category_path).map((part, index) => (
                        <span key={`${part}-${index}`}>
                          {index > 0 && <i aria-hidden="true">›</i>}
                          {part}
                        </span>
                      ))}
                    </div>
                  </div>
                )}
                <dl className="tx-preview-meta">
                  <div><dt>작성 기관</dt><dd>{selectedTable.org_name || "—"}</dd></div>
                  <div>
                    <dt>수록 기준</dt>
                    <dd>{formatLatestPeriod(selectedTable.latest_period)}</dd>
                  </div>
                </dl>
                <a
                  className="tx-preview-link"
                  href={selectedTable.kosis_url}
                  target="_blank"
                  rel="noreferrer"
                >
                  <LinkIcon /> KOSIS 원문 열기
                </a>
              </>
            ) : (
              <div className="tx-preview-empty">
                <ChartIcon size="1.5em" />
                <strong>통계표 미리보기</strong>
                <span>목록에서 통계표를 선택해 주세요.</span>
              </div>
            )}
          </aside>
        </div>
      </div>
    </div>
  );
}

export default TableExplorer;
