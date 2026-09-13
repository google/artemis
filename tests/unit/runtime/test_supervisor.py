from unittest.mock import MagicMock, patch

from artemis.runtime.supervisor import ProcessSupervisor


def test_verified_termination_rejects_reused_pid():
    process = MagicMock()
    process.create_time.return_value = 200.0
    with (
        patch("psutil.Process", return_value=process),
        patch.object(ProcessSupervisor, "terminate_tree") as terminate_tree,
    ):
        assert ProcessSupervisor.terminate_tree_verified(1234, 100.0) is False

    terminate_tree.assert_not_called()


def test_verified_termination_stops_matching_process_tree():
    process = MagicMock()
    process.create_time.return_value = 100.0
    with (
        patch("psutil.Process", return_value=process),
        patch.object(ProcessSupervisor, "terminate_tree", return_value=True) as terminate_tree,
    ):
        assert ProcessSupervisor.terminate_tree_verified(1234, 100.0) is True

    terminate_tree.assert_called_once_with(1234, timeout_seconds=3.0)
