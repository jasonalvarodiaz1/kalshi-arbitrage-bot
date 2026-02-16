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


def size_position(bankroll: float, win_prob: float, contract_price_cents: int,
                  max_trade_usd: float, kelly_multiplier: float = 0.5) -> int:
    """
    Calculate number of contracts to buy using Kelly Criterion.
    
    Args:
        bankroll: Current available balance in USD
        win_prob: Estimated probability of winning (0 to 1)
        contract_price_cents: Price per contract in cents
        max_trade_usd: Maximum single trade size cap
        kelly_multiplier: Fraction of Kelly to use (0.5 = half-Kelly, conservative)
    
    Returns: 
        Number of contracts (integer, 0 if no positive edge)
    """
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
    
    # Apply maximum trade limit
    bet_size_usd = min(bet_size_usd, max_trade_usd)
    
    # Convert to number of contracts
    num_contracts = int(bet_size_usd / contract_price_usd)
    
    # Return calculated quantity (may be 0 if Kelly sizing is too conservative)
    return num_contracts
