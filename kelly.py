"""Kelly Criterion position sizing for Kalshi trading bot."""


def kelly_fraction(win_prob: float, win_amount: float, loss_amount: float) -> float:
    """
    Calculate optimal Kelly fraction of bankroll to bet.
    
    For a 92¢ YES contract with 96% estimated win rate:
      win_prob = 0.96
      win_amount = 0.08 (profit if win)
      loss_amount = 0.92 (loss if lose)
      b = win_amount / loss_amount = 0.087
      kelly = (0.96 * 0.087 - 0.04) / 0.087 ≈ 0.50
      
    Args:
        win_prob: Probability of winning (0 to 1)
        win_amount: Amount won if successful
        loss_amount: Amount lost if unsuccessful
        
    Returns:
        Kelly fraction (0 to 1). Returns 0 if no edge or negative edge.
    """
    if loss_amount <= 0 or win_prob <= 0 or win_prob >= 1:
        return 0.0
    
    # Kelly formula: f = (bp - q) / b
    # where b = odds received (win_amount / loss_amount)
    #       p = win probability
    #       q = loss probability (1 - p)
    b = win_amount / loss_amount
    q = 1 - win_prob
    
    kelly = (b * win_prob - q) / b
    
    # No negative bets
    return max(0.0, kelly)


def size_position(
    bankroll: float = None,
    win_prob: float = None,
    contract_price_cents: int = 50,
    max_trade_usd: float = None,
    kelly_multiplier: float = 0.5,
    # New parameters
    edge: float = None,
    max_fraction: float = 0.05,
    min_contracts: int = 1,
    liquidity_penalty: float = 1.0,
    duration_minutes: float = None,
) -> int:
    """Kelly Criterion position sizing with liquidity and duration adjustments.
    
    Supports both legacy and new API:
    - Legacy: size_position(bankroll, win_prob, contract_price_cents, max_trade_usd, kelly_multiplier)
    - New: size_position(edge=0.03, bankroll=1000, contract_price_cents=50, liquidity_penalty=0.8, duration_minutes=15)
    
    Args:
        bankroll: Available capital in dollars (required)
        win_prob: Probability of winning (for arb, this is ~1.0) - legacy API
        contract_price_cents: Price per contract in cents
        max_trade_usd: Maximum single trade size cap
        kelly_multiplier: Fraction of Kelly to use (0.5 = half-Kelly, conservative)
        edge: Expected edge as decimal (e.g., 0.03 for 3%) - new API
        max_fraction: Maximum fraction of bankroll per trade (default 5%)
        min_contracts: Minimum position size
        liquidity_penalty: Multiplier 0-1 based on orderbook depth.
            1.0 = deep book, no penalty. 0.5 = thin book, halve position.
        duration_minutes: Time to expiry. Shorter = smaller position.
            If None, no duration adjustment.
    
    Returns:
        Number of contracts to trade
    """
    # Handle legacy API - if called with positional args
    if bankroll is not None and win_prob is not None and edge is None:
        # Legacy mode: use old calculation
        if bankroll <= 0 or contract_price_cents <= 0 or win_prob <= 0:
            return 0
        
        # Calculate payoff structure for a contract
        contract_price_usd = contract_price_cents / 100.0
        payout_usd = 1.0  # Kalshi contracts pay $1 on win
        
        # Win/loss amounts
        win_amount = payout_usd - contract_price_usd  # Profit if win
        loss_amount = contract_price_usd  # Loss if lose
        
        if win_amount <= 0:
            return 0  # No positive edge
        
        # Calculate Kelly fraction
        kelly = kelly_fraction(win_prob, win_amount, loss_amount)
        
        if kelly <= 0:
            return 0  # Kelly says don't bet
        
        # Apply conservative multiplier (half-Kelly recommended)
        fraction_to_bet = kelly * kelly_multiplier
        
        # Calculate bet size in USD
        bet_size_usd = bankroll * fraction_to_bet
        
        # Apply maximum trade limit if provided
        if max_trade_usd is not None:
            bet_size_usd = min(bet_size_usd, max_trade_usd)
        
        # Convert to number of contracts
        num_contracts = int(bet_size_usd / contract_price_usd)
        
        # Return calculated quantity (may be 0 if Kelly sizing is too conservative)
        return num_contracts
    
    # New API mode
    if edge is None or bankroll is None:
        return min_contracts
    
    if edge <= 0 or bankroll <= 0:
        return min_contracts
    
    # Kelly fraction: f* = edge / odds
    # For binary markets: odds = (1 - price) / price
    # For arbitrage with known edge, we use a simplified approach
    if win_prob is None:
        win_prob = 0.99  # Assume near-certain for arb
    
    loss_prob = 1 - win_prob
    if loss_prob <= 0:
        kelly_fraction_val = max_fraction  # Pure arb — use max
    else:
        odds = win_prob / loss_prob
        kelly_fraction_val = edge - (1 - edge) / odds if odds > 0 else 0
    
    # Apply fractional Kelly (half Kelly is standard for safety)
    kelly_fraction_val *= kelly_multiplier
    
    # Cap at max_fraction
    kelly_fraction_val = min(kelly_fraction_val, max_fraction)
    
    # Apply liquidity penalty
    kelly_fraction_val *= max(0.1, min(1.0, liquidity_penalty))
    
    # Apply duration factor — shorter markets get smaller positions
    if duration_minutes is not None and duration_minutes > 0:
        # Scale: 15 min = 0.5x, 60 min = 0.8x, 240+ min = 1.0x
        duration_factor = min(1.0, 0.4 + (duration_minutes / 400))
        kelly_fraction_val *= duration_factor
    
    # Convert to contracts
    dollar_amount = bankroll * kelly_fraction_val
    
    # Apply max_trade_usd limit if provided
    if max_trade_usd is not None:
        dollar_amount = min(dollar_amount, max_trade_usd)
    
    contract_price_dollars = contract_price_cents / 100
    if contract_price_dollars <= 0:
        return min_contracts
    
    contracts = int(dollar_amount / contract_price_dollars)
    return max(contracts, min_contracts)
