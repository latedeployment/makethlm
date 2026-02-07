from app import add, divide, factorial


def test_add():
    assert add(2, 3) == 5


def test_add_negative():
    assert add(-1, 1) == 0


def test_divide():
    assert divide(10, 2) == 5.0


def test_divide_by_zero():
    # This will fail — divide() doesn't handle zero
    try:
        divide(1, 0)
        assert False, "Should have raised an error"
    except ZeroDivisionError:
        pass


def test_factorial():
    assert factorial(5) == 120


def test_factorial_zero():
    assert factorial(0) == 1
