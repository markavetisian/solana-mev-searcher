const BN = require("bn.js");
const { PublicKey } = require("@solana/web3.js");
const p = require("@pump-fun/pump-sdk");
const s = require("@pump-fun/pump-swap-sdk");
const tiersRows = [["0","2","93","30"],["420000000000","20","5","95"],["1470000000000","20","5","90"],["2460000000000","20","5","85"],["3440000000000","20","5","80"],["4420000000000","20","5","75"],["9820000000000","20","5","70"],["14740000000000","20","5","65"],["19650000000000","20","5","60"],["24560000000000","20","5","55"],["29470000000000","20","5","50"],["34380000000000","20","5","45"],["39300000000000","20","5","40"],["44210000000000","20","5","35"],["49120000000000","20","5","30"],["54030000000000","20","5","28"],["58940000000000","20","5","25"],["63860000000000","20","5","23"],["68770000000000","20","5","20"],["73681000000000","20","5","18"],["78590000000000","20","5","15"],["83500000000000","20","5","13"],["88400000000000","20","5","10"],["93330000000000","20","5","8"],["98240000000000","20","5","5"]];
const fees = (r)=>({lpFeeBps:new BN(r[1]),protocolFeeBps:new BN(r[2]),creatorFeeBps:new BN(r[3])});
const feeConfig = { feeTiers: tiersRows.map(r=>({marketCapLamportsThreshold:new BN(r[0]), fees:fees(r)})), stableFeeTiers: [], flatFees: {lpFeeBps:new BN(25),protocolFeeBps:new BN(5),creatorFeeBps:new BN(0)}, exoticFlatFees:{lpFeeBps:new BN(0),protocolFeeBps:new BN(0),creatorFeeBps:new BN(0)} };
const global = { feeBasisPoints:new BN(95), creatorFeeBasisPoints:new BN(5), creatorFeeConfigurable:false, initialVirtualTokenReserves:new BN("1073000000000000"), initialVirtualSolReserves:new BN("30000000000"), initialRealTokenReserves:new BN("793100000000000"), tokenTotalSupply:new BN("1000000000000000"), mayhemModeEnabled:false, whitelistedQuoteMints:[] };
let seed = 12345; const rnd=()=>{seed=(seed*1103515245+12345)%2147483648; return seed/2147483648;};
const creator = new PublicKey("Coyj3LtKn1BNSgWc9HsGK5SKoGfEoDaymig4wrN6pump");
const out = {curve:[], amm:[]};
const supply = new BN("1000000000000000");
for (let i=0;i<300;i++){
  // walk the curve: bought fraction 0..0.99
  const frac = rnd()*0.99;
  const rt = new BN(Math.floor(793100000000000*(1-frac)).toString());
  const sold = new BN("793100000000000").sub(rt);
  const vt = new BN("1073000000000000").sub(sold);
  const k = new BN("1073000000000000").mul(new BN("30000000000"));
  const vq = k.div(vt).addn(Math.floor(rnd()*1000));
  const rq = vq.sub(new BN("30000000000"));
  const hasCreator = rnd()<0.85;
  const bc = { virtualTokenReserves:vt, virtualQuoteReserves:vq, realTokenReserves:rt, realQuoteReserves:rq, tokenTotalSupply:supply, complete:false, creator: hasCreator?creator:PublicKey.default, isMayhemMode:false, isCashbackCoin:false, quoteMint:PublicKey.default, creatorFeeBps:new BN(0), canEditCreatorFee:false, isHolderReward:false };
  const spend = new BN(Math.floor(1e6 + rnd()*5e9).toString());
  const tokensOut = p.getBuyTokenAmountFromSolAmount({global, feeConfig, mintSupply:supply, bondingCurve:bc, amount:spend, quoteMint:PublicKey.default});
  const costTokens = p.getBuySolAmountFromTokenAmount({global, feeConfig, mintSupply:supply, bondingCurve:bc, amount:tokensOut, quoteMint:PublicKey.default});
  const sellAmt = BN.min(new BN(Math.floor(rnd()*5e13).toString()).addn(1000), sold.gtn(0)?sold:new BN(1000));
  let sellOut = null; try { sellOut = p.getSellSolAmountFromTokenAmount({global, feeConfig, mintSupply:supply, bondingCurve:bc, amount:sellAmt}).toString(); } catch(e){}
  out.curve.push({vt:vt.toString(), vq:vq.toString(), rt:rt.toString(), rq:rq.toString(), has_creator:hasCreator, spend:spend.toString(), tokens_out:tokensOut.toString(), cost_for_tokens:costTokens.toString(), sell_amount:sellAmt.toString(), sell_out:sellOut});
}
const baseMint = new PublicKey("Coyj3LtKn1BNSgWc9HsGK5SKoGfEoDaymig4wrN6pump");
const poolCreator = s.pumpPoolAuthorityPda(baseMint);
const globalConfig = { lpFeeBasisPoints:new BN(20), protocolFeeBasisPoints:new BN(5), coinCreatorFeeBasisPoints:new BN(5), creatorFeeConfigurable:false };
for (let i=0;i<300;i++){
  const base = new BN(Math.floor(5e13 + rnd()*2.0e14).toString());
  const quote = new BN(Math.floor(5e10 + rnd()*1e12).toString());
  const canonical = rnd()<0.8; const hasCreator = rnd()<0.85;
  const common = { slippage:1, baseReserve:base, quoteReserve:quote, globalConfig, baseMintAccount:{supply:BigInt("1000000000000000")}, baseMint, coinCreator: hasCreator?creator:PublicKey.default, creator: canonical?poolCreator:creator, feeConfig };
  const qin = new BN(Math.floor(1e6 + rnd()*5e9).toString());
  const b = s.buyQuoteInput({...common, quote:qin});
  const bin = new BN(Math.floor(1e9 + rnd()*1e13).toString());
  const sl = s.sellBaseInput({...common, base:bin});
  const bbo = s.buyBaseInput({...common, base:bin});
  out.amm.push({base:base.toString(), quote:quote.toString(), canonical, has_creator:hasCreator, quote_in:qin.toString(), base_out:b.base.toString(), base_in:bin.toString(), sell_ui_quote:sl.uiQuote.toString(), buy_base_ui_quote:bbo.uiQuote.toString()});
}
require("fs").writeFileSync("golden.json", JSON.stringify(out));
console.log("ok", out.curve.length, out.amm.length, out.curve[0], out.amm[0]);
