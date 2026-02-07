"""A small calculator module."""


def add(a, b):
    return a + b


def divide(a, b):
    return a / b


def factorial(n):
    if n <= 1:
        return 1
    return n * factorial(n - 1)
