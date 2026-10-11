def quote(subtotal, remote=False):
    """Nonnegative subtotals ship for 5 below 100, otherwise 0; remote adds 8."""
    if subtotal < 0:
        raise ValueError("subtotal must be nonnegative")
    base = 5 if subtotal < 100 else 0
    return base + (8 if remote else 0)
