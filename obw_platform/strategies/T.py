//@version=5
strategy("Cryptomine (Royal Q) — TOR v5 (true level fills, no overbuy)",
     overlay=true,
     pyramiding=300,
     initial_capital=10000,
     default_qty_type=strategy.cash,
     commission_type=strategy.commission.percent,
     commission_value=0.0,
     process_orders_on_close=true,
     calc_on_order_fills=true)

// =====================
// Inputs (TOR)
// =====================
firstBuyUSDT      = input.float(5.0,  "First Buy (USDT)", minval=0.1)
tpPercent         = input.float(1.1,  "TP % (Whole warehouse)", step=0.1)
callbackPercent   = input.float(0.2,  "Callback % (Trailing)", step=0.05)
marginCallLimit   = input.int(244,    "Margin Call Limit (max buys)", minval=1)
linearDropPercent = input.float(0.5,  "Margin Call Drop % (after lvl 5)", step=0.1)

autoMerge         = input.bool(true, "Auto Merge")
subSellTPPercent  = input.float(1.3, "Sub-sell TP % (last lot)", step=0.1)

// Nonlinear initial drops (lvl2..lvl6)
drop1 = input.float(0.3, "Nonlinear drop lvl2 %", step=0.1)
drop2 = input.float(0.4, "Nonlinear drop lvl3 %", step=0.1)
drop3 = input.float(0.6, "Nonlinear drop lvl4 %", step=0.1)
drop4 = input.float(0.8, "Nonlinear drop lvl5 %", step=0.1)
drop5 = input.float(0.8, "Nonlinear drop lvl6 %", step=0.1)

// Multipliers (lvl2..lvl5), after lvl5 -> 1x
mult2 = input.float(1.5, "Multiplier lvl2", step=0.1)
mult3 = input.float(1.0, "Multiplier lvl3", step=0.1)
mult4 = input.float(2.0, "Multiplier lvl4", step=0.1)
mult5 = input.float(3.5, "Multiplier lvl5", step=0.1)

maxFillsPerBar    = input.int(6,  "Max DCA fills per bar", minval=1, maxval=20)
maxSubSellsPerBar = input.int(10, "Max SUB-sells per bar", minval=1, maxval=50)


// =====================
// Helper funcs (pure)
// =====================
f_getDropForNextLevel(_numBuys) =>
    nb = _numBuys + 1
    nb == 2 ? drop1 :
     nb == 3 ? drop2 :
     nb == 4 ? drop3 :
     nb == 5 ? drop4 :
     nb == 6 ? drop5 :
     linearDropPercent

f_getMultForNextLevel(_numBuys) =>
    nb = _numBuys + 1
    nb == 2 ? mult2 :
     nb == 3 ? mult3 :
     nb == 4 ? mult4 :
     nb == 5 ? mult5 :
     1.0

f_nextLevel(_lastFillPrice, _numBuys) =>
    d = f_getDropForNextLevel(_numBuys)
    _lastFillPrice * (1.0 - d/100.0)

f_recalcAvg(_cost, _size) =>
    _size > 0 ? _cost / _size : na


// =====================
// State
// =====================
var float posSize        = 0.0
var float posCostUSDT    = 0.0
var float avgPrice       = na
var int   numBuys        = 0
var float lastFillPrice  = na
var float nextLevelPrice = na

// LIFO lots
var int lotCounter    = 0
var string[] lotIds   = array.new_string()
var float[]  lotQty   = array.new_float()
var float[]  lotPrice = array.new_float()

// trailing for full TP
var bool  trailingActive = false
var float trailingMax    = na

// cycle flags
var bool resetCycle = false
var bool restartedThisBar = false
restartedThisBar := false


// =====================
// RESET + immediate restart (Kostya style)
// =====================
if resetCycle
    // wipe internal warehouse
    posSize := 0
    posCostUSDT := 0
    avgPrice := na
    numBuys := 0
    lastFillPrice := na
    nextLevelPrice := na
    trailingActive := false
    trailingMax := na
    array.clear(lotIds), array.clear(lotQty), array.clear(lotPrice)
    resetCycle := false

    // immediate Buy_0 same bar
    buyUSDT = firstBuyUSDT
    lotCounter += 1
    id0 = "L" + str.tostring(lotCounter)

    // market-style entry at close
    strategy.entry(id0, strategy.long, qty=buyUSDT, comment="Restart Buy_0")
    qty0 = buyUSDT / close
    fill0 = close

    array.push(lotIds, id0)
    array.push(lotQty, qty0)
    array.push(lotPrice, fill0)

    posSize     += qty0
    posCostUSDT += buyUSDT
    avgPrice    := fill0
    numBuys     := 1
    lastFillPrice := fill0
    nextLevelPrice := f_nextLevel(lastFillPrice, numBuys)

    restartedThisBar := true


// =====================
// Start new cycle if flat
// =====================
if posSize == 0 and array.size(lotIds) == 0 and not resetCycle and not restartedThisBar
    buyUSDT = firstBuyUSDT
    lotCounter += 1
    id0 = "L" + str.tostring(lotCounter)

    strategy.entry(id0, strategy.long, qty=buyUSDT, comment="First Buy_0")
    qty0 = buyUSDT / close
    fill0 = close

    array.push(lotIds, id0)
    array.push(lotQty, qty0)
    array.push(lotPrice, fill0)

    posSize     += qty0
    posCostUSDT += buyUSDT
    avgPrice    := fill0
    numBuys     := 1
    lastFillPrice := fill0
    nextLevelPrice := f_nextLevel(lastFillPrice, numBuys)


// =====================
// FULL TP (priority)
// =====================
tpPrice = avgPrice * (1.0 + tpPercent/100.0)
tpHit   = not na(avgPrice) and close >= tpPrice

if tpHit
    if callbackPercent > 0
        trailingActive := true
        trailingMax := na(trailingMax) ? close : math.max(trailingMax, close)
        trailStop = trailingMax * (1.0 - callbackPercent/100.0)

        if close <= trailStop
            strategy.close_all(comment="TP Full (Trailing)")
            // ✅ yellow flag on full sell
            label.new(bar_index, close, "FULL SELL",
                 style=label.style_label_down,
                 color=color.yellow, textcolor=color.black, size=size.small)
            resetCycle := true
    else
        strategy.close_all(comment="TP Full")
        // ✅ yellow flag on full sell
        label.new(bar_index, close, "FULL SELL",
             style=label.style_label_down,
             color=color.yellow, textcolor=color.black, size=size.small)
        resetCycle := true


// =====================
// DCA buys (TRUE LEVEL FILLS)
// - only if TP not hit
// - skip DCA on restart bar
// - each buy is a LIMIT at nextLevelPrice
// - fill price assumed = nextLevelPrice
// =====================
if not tpHit and posSize > 0 and not restartedThisBar
    canBuyMore = numBuys < marginCallLimit
    fills = 0

    while canBuyMore and fills < maxFillsPerBar and not na(nextLevelPrice) and low <= nextLevelPrice
        mult = f_getMultForNextLevel(numBuys)
        buyUSDT = firstBuyUSDT * mult

        lotCounter += 1
        id = "L" + str.tostring(lotCounter)

        // LIMIT entry at exact level
        strategy.entry(id, strategy.long, qty=buyUSDT, limit=nextLevelPrice, comment="DCA Buy")

        fillP   = nextLevelPrice
        qtyB    = buyUSDT / fillP

        array.push(lotIds, id)
        array.push(lotQty, qtyB)
        array.push(lotPrice, fillP)

        posSize     += qtyB
        posCostUSDT += buyUSDT
        avgPrice    := f_recalcAvg(posCostUSDT, posSize)

        numBuys      += 1
        lastFillPrice := fillP
        nextLevelPrice := f_nextLevel(lastFillPrice, numBuys)

        trailingActive := false
        trailingMax := na

        fills += 1
        canBuyMore := numBuys < marginCallLimit

// =====================
// SUB-SELL (only after >5 buys, LIFO, cascade)
// =====================
if not tpHit and posSize > 0 and numBuys > 5 and not restartedThisBar
    sold = 0
    anySold = false

    while sold < maxSubSellsPerBar
        lastIdx = array.size(lotIds) - 1
        if lastIdx < 0
            break

        idLast    = array.get(lotIds, lastIdx)
        qtyLast   = array.get(lotQty, lastIdx)
        entryLast = array.get(lotPrice, lastIdx)   // або lotEntry у твоїй версії

        lastLotTP = entryLast * (1.0 + subSellTPPercent/100.0)

        if close >= lastLotTP
            anySold := true
            strategy.close(idLast, comment="Sub-sell last lot")

            entryCostUSDT = qtyLast * entryLast
            profitUSDT    = qtyLast * (close - entryLast)

            // remove lot from warehouse
            posSize -= qtyLast
            posCostUSDT -= entryCostUSDT
            if autoMerge
                posCostUSDT -= profitUSDT

            avgPrice := f_recalcAvg(posCostUSDT, posSize)

            array.pop(lotIds), array.pop(lotQty), array.pop(lotPrice)

            // Kostya-style: shrink step count
            numBuys := math.max(numBuys - 1, 0)

            sold += 1

            if array.size(lotIds) == 0 or posSize <= 0
                resetCycle := true
                break
        else
            break

    // IMPORTANT: after selling tail, re-anchor grid to new last lot
    if anySold and not resetCycle and array.size(lotPrice) > 0
        lastFillPrice := array.get(lotPrice, array.size(lotPrice) - 1)
        nextLevelPrice := f_nextLevel(lastFillPrice, numBuys)


// =====================
// Plots
// =====================
plot(avgPrice, "Avg Price", color=color.new(color.yellow, 0), linewidth=2)
plot(tpPrice,  "TP Price",  color=color.new(color.green, 0), linewidth=2)
plot(nextLevelPrice, "Next DCA Level", color=color.new(color.red, 0), style=plot.style_cross)
plotchar(numBuys, "Buys Count", "", location=location.top)
