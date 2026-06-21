from scripts.repowire_reap_orphans import RepowireProc, find_orphans


def _proc(pid, kind, peer_id, pane):
    return RepowireProc(pid=pid, kind=kind, peer_id=peer_id, pane=pane)


def test_live_hook_with_live_pane_and_live_peer_is_not_orphan():
    procs = [_proc(100, "ws_hook", "repow-x", "%328")]
    orphans = find_orphans(procs, live_panes={"%328"}, live_peer_ids={"repow-x"})
    assert orphans == []


def test_hook_with_dead_pane_and_dead_peer_is_orphan():
    procs = [_proc(1395918, "ws_hook", "repow-default-2bb40e47", None)]
    orphans = find_orphans(procs, live_panes={"%328"}, live_peer_ids={"repow-x"})
    assert [o.pid for o in orphans] == [1395918]


def test_pane_alive_but_peer_dead_is_NOT_orphan_conservative():
    # И-условие: пока панель жива — не трогаем, даже если peer не в реестре
    procs = [_proc(200, "mcp", "repow-y", "%5")]
    orphans = find_orphans(procs, live_panes={"%5"}, live_peer_ids=set())
    assert orphans == []


def test_peer_alive_but_pane_dead_is_NOT_orphan_conservative():
    procs = [_proc(201, "mcp", "repow-z", "%999")]
    orphans = find_orphans(procs, live_panes=set(), live_peer_ids={"repow-z"})
    assert orphans == []


def test_proc_without_peer_id_is_skipped():
    procs = [_proc(202, "ws_hook", None, None)]
    orphans = find_orphans(procs, live_panes=set(), live_peer_ids=set())
    assert orphans == []
