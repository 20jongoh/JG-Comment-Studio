import pytest

from run import single_instance


def test_second_server_cannot_recover_the_first_servers_active_jobs(tmp_path):
    with single_instance(tmp_path):
        with pytest.raises(SystemExit, match="이미 실행 중"):
            with single_instance(tmp_path):
                pytest.fail("A second server acquired the same runtime directory")
    with single_instance(tmp_path):
        pass  # The OS lock is released when the first process's handle closes.
