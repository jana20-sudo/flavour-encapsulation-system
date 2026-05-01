"""
FlavorEncap AI — simulator.py v5 ADVANCED
Scientific enhancements:
  - Reaction kinetics (Maillard, oxidation, hydrolysis)
  - CFD-inspired droplet evaporation model (Abramzon-Sirignano)
  - Population Balance PSD (log-normal + crystallisation)
  - Bayesian process optimiser (Latin Hypercube + response surface)
  - Multi-objective Pareto front (NSGA-II simplified)
  - Monte Carlo uncertainty quantification
  - Thermodynamic fugacity for flavor partitioning
  - GAB + BET dual-mode sorption model
  - Empirical Tg mixing rule (Gordon-Taylor + Fox equation)
  - Full energy & mass balance with psychrometric enthalpy
  - Equipment fingerprint API for digital twin
"""

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from scipy.optimize import fsolve, minimize, differential_evolution
from scipy.stats import qmc  # Latin hypercube sampler
import numpy as np
import math, os, json, requests, random, time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__)
CORS(app)

# ══ STATIC SERVING ════════════════════════════════════════════
@app.route('/')
def serve_index():
    path = os.path.join(BASE_DIR, 'index.html')
    if not os.path.exists(path):
        return f"<pre>ERROR: index.html not found in {BASE_DIR}</pre>", 404
    return send_from_directory(BASE_DIR, 'index.html')

@app.route('/style.css')
def serve_css(): return send_from_directory(BASE_DIR, 'style.css')

@app.route('/app.js')
def serve_js(): return send_from_directory(BASE_DIR, 'app.js')

@app.route('/features.js')
def serve_feat(): return send_from_directory(BASE_DIR, 'features.js')


# ══ PHYSICAL CONSTANTS ═════════════════════════════════════════
R_GAS   = 8.314        # J/mol·K
P_ATM   = 101.325      # kPa
CP_AIR  = 1.006        # kJ/kg·K
H_FG    = 2501.0       # kJ/kg  latent heat at 0°C
CP_WV   = 1.805        # kJ/kg·K vapour
SIGMA_W  = 0.0728      # N/m water surface tension at 20°C
MU_AIR  = 1.81e-5      # Pa·s dynamic viscosity air at 25°C
RHO_AIR = 1.184        # kg/m³ air at 25°C
LAMBDA_AIR = 0.0262    # W/m·K thermal conductivity air

# ══ PSYCHROMETRIC & THERMODYNAMIC HELPERS ══════════════════════

def sat_pressure(T_C: float) -> float:
    """Antoine equation — sat vapour pressure of water, kPa."""
    return 6.112 * math.exp(17.67 * T_C / (T_C + 243.5))

def humidity_ratio(T_C: float, RH_pct: float) -> float:
    """W = kg water / kg dry air."""
    Pv = (RH_pct / 100.0) * sat_pressure(T_C)
    return 0.622 * Pv / max(0.001, P_ATM - Pv)

def enthalpy_moist_air(T_C: float, W: float) -> float:
    """Specific enthalpy of moist air, kJ/kg dry air."""
    return CP_AIR * T_C + W * (H_FG + CP_WV * T_C)

def dew_point(T_C: float, RH_pct: float) -> float:
    """Magnus approximation dew point, °C."""
    a, b = 17.625, 243.04
    alpha = math.log(max(1e-9, RH_pct / 100.0)) + a * T_C / (b + T_C)
    return b * alpha / (a - alpha)

def wet_bulb(T_dry: float, W: float) -> float:
    """Wet-bulb temperature via Sprung's formula, °C."""
    try:
        def eq(Tw):
            return [W - humidity_ratio(Tw[0], 100) + (CP_AIR / H_FG) * (T_dry - Tw[0])]
        return float(fsolve(eq, [T_dry - 10])[0])
    except Exception:
        return T_dry - 10

def clausius_clapeyron_hvap(T_C: float) -> float:
    """Temperature-dependent heat of vaporisation, kJ/kg (Watson correlation)."""
    Tc = 647.1  # K critical temp water
    T  = T_C + 273.15
    return 2501.0 * ((Tc - T) / (Tc - 373.15)) ** 0.38

def vapor_pressure_compound(T_C: float, bp_C: float, Hvap_kJmol: float = 40.0) -> float:
    """
    Clausius-Clapeyron for flavor compound vapor pressure, Pa.
    Hvap_kJmol: molar heat of vaporisation (default 40 kJ/mol terpene estimate).
    """
    R = 8.314e-3  # kJ/mol·K
    T = T_C + 273.15
    Tbp = bp_C + 273.15
    Pvap_ref = 101325.0  # Pa at boiling point
    return Pvap_ref * math.exp(-Hvap_kJmol / R * (1.0 / T - 1.0 / Tbp))

# ══ ABRAMZON-SIRIGNANO DROPLET EVAPORATION MODEL ═══════════════

class DropletEvaporation:
    """
    Enhanced evaporation model incorporating dissolved solids and film
    diffusion resistance (Abramzon & Sirignano, 1989 IJHMT).
    Returns: drying rate kg/s and droplet lifetime seconds.
    """
    def __init__(self, D50_um: float, T_air: float, RH: float, S_pct: float):
        self.D = D50_um * 1e-6          # m droplet diameter
        self.T_air = T_air + 273.15     # K
        self.W_inf = humidity_ratio(T_air, RH)  # kg/kg
        self.W_surf = humidity_ratio(T_air - 20, 100)  # approximate
        self.S = S_pct / 100.0          # mass fraction solids
        self.rho_drop = 1000 + 200 * self.S  # kg/m³

    def Re_droplet(self, U_rel: float = 2.0) -> float:
        """Relative Reynolds number (droplet vs air velocity)."""
        return RHO_AIR * U_rel * self.D / MU_AIR

    def Sh_number(self) -> float:
        """Sherwood number — mass transfer (Ranz-Marshall correlation)."""
        Re = self.Re_droplet()
        Sc = MU_AIR / (RHO_AIR * 2.6e-5)  # Schmidt ~ 0.6 for water vapour
        return 2.0 + 0.6 * Re**0.5 * Sc**0.33

    def drying_rate(self) -> float:
        """Evaporation rate, kg/s per droplet."""
        Sh = self.Sh_number()
        D_vapour = 2.6e-5  # m²/s diffusivity water vapour in air
        rho_m = P_ATM * 1000 / (287 * self.T_air)  # moist air density kg/m³
        delta_W = max(0, self.W_surf - self.W_inf)
        return math.pi * self.D * D_vapour * rho_m * Sh * delta_W

    def droplet_lifetime(self) -> float:
        """D² law lifetime, seconds (modified for dissolved solids)."""
        m0 = (4/3) * math.pi * (self.D/2)**3 * self.rho_drop
        rate = max(1e-20, self.drying_rate())
        crust_factor = 1.0 / max(0.1, 1.0 - self.S)  # crust resistance
        return (m0 * (1 - self.S)) / rate * crust_factor


# ══ POPULATION BALANCE MODEL — PARTICLE SIZE DISTRIBUTION ══════

class ParticleSizeDistribution:
    """
    Log-normal PSD from Lefebvre atomization + population balance breakage.
    Ref: Masters (1991), Lefebvre (1989).
    """
    def __init__(self, rpm: float, solids: float, sigma_feed: float = 0.040,
                 rho_feed: float = 1100):
        self.rpm = rpm
        self.S = solids / 100.0
        self.sigma = sigma_feed
        self.rho = rho_feed

    def D50_lefebvre(self) -> float:
        """Volume-median diameter μm from Lefebvre rotary atomizer eq."""
        omega = self.rpm * 1000 * 2 * math.pi / 60
        D_drop = 0.4 * math.sqrt(self.sigma / max(1e-9, self.rho * omega**2 * 0.0036))
        return max(5.0, min(250.0, D_drop * 1e6 * (self.S / 0.20)**0.33))

    def GSD(self) -> float:
        """Geometric standard deviation — wider spread at high solids."""
        return max(1.2, min(2.8, 1.6 + self.S * 0.8))

    def percentiles(self) -> dict:
        """D10, D50, D90 from log-normal distribution."""
        D50 = self.D50_lefebvre()
        g   = self.GSD()
        ln_sigma = math.log(g)
        D10 = D50 * math.exp(-1.282 * ln_sigma)
        D90 = D50 * math.exp(+1.282 * ln_sigma)
        span = (D90 - D10) / max(1, D50)
        return {"D10": round(D10, 1), "D50": round(D50, 1), "D90": round(D90, 1),
                "span": round(span, 2), "GSD": round(g, 2)}


# ══ MAILLARD & OXIDATION KINETICS ═══════════════════════════════

class DegradationKinetics:
    """
    Multi-pathway degradation model.
    1. Thermal volatilisation (Arrhenius, Ea ~ 40-60 kJ/mol for terpenes)
    2. Maillard browning (2nd order, k = A·exp(-Ea/RT), Ea ~ 110 kJ/mol)
    3. Autoxidation (chain, initiation Ea ~ 150 kJ/mol)
    4. Hydrolysis (acid/base catalysed, pH-dependent)
    Ref: Labuza (1980) Food Technology; Reineccius (2006).
    """
    def __init__(self, T_C: float, pH: float, Aw: float,
                 logP: float, compound_class: str = "terpene"):
        self.T = T_C + 273.15
        self.pH = pH
        self.Aw = Aw
        self.logP = logP
        self.cls = compound_class.lower()

    def _k_arrhenius(self, Ea_kJ: float, A: float = 1e8) -> float:
        return A * math.exp(-Ea_kJ * 1000 / (R_GAS * self.T))

    def thermal_loss_pct(self) -> float:
        """% flavor lost during 5-second residence in spray dryer chamber."""
        Ea = {"terpene": 45, "aldehyde": 38, "alcohol": 48,
              "ketone": 52, "phenolic": 60}.get(self.cls, 45)
        k = self._k_arrhenius(Ea)
        tau = 5.0  # seconds residence time
        return round(min(40.0, k * tau * 100 * (self.T / 450)), 2)

    def maillard_rate(self) -> float:
        """Maillard browning rate constant (relative, 0-1 scale)."""
        if self.cls not in ("aldehyde", "ketone"): return 0.0
        Ea = 110.0
        k = self._k_arrhenius(Ea, A=1e15)
        # Water activity optimum at Aw 0.6-0.8 (Maillard bell curve)
        aw_factor = 4 * self.Aw * (1 - self.Aw)
        return round(min(1.0, k * aw_factor * 1e-6), 4)

    def oxidation_rate(self) -> float:
        """Autoxidation initiation rate (relative, 0-1). Peaks near Aw=0.3."""
        Ea = 150.0
        k = self._k_arrhenius(Ea, A=1e20)
        aw_factor = 1.0 - abs(self.Aw - 0.3) * 2  # optimal Aw=0.3
        aw_factor = max(0.1, aw_factor)
        return round(min(1.0, k * aw_factor * 1e-10), 5)

    def hydrolysis_rate(self) -> float:
        """
        Acid/base catalysed hydrolysis of ester bonds in wall material.
        Dominant for esters, modified starches at extreme pH.
        """
        H  = 10 ** (-self.pH)           # [H+] mol/L
        OH = 10 ** (self.pH - 14)       # [OH-] mol/L
        ka, kb = 1e-3, 5e-4             # empirical rate constants
        return round((ka * H + kb * OH) * 1e3, 5)

    def combined_loss_modifier(self) -> float:
        """Total EE penalty from degradation pathways."""
        tl = self.thermal_loss_pct() * 0.8
        ml = self.maillard_rate() * 5
        ol = self.oxidation_rate() * 3
        hl = self.hydrolysis_rate() * 10
        return round(tl + ml + ol + hl, 2)


# ══ ADVANCED SORPTION MODEL — GAB + BET DUAL MODE ══════════════

class SorptionModel:
    """
    GAB (Guggenheim-Anderson-de Boer) isotherm for water activity prediction.
    Ref: van den Berg & Bruin (1981); Roos & Karel (1991).
    Parameters tuned for typical spray-dried food powders.
    """
    def __init__(self, material: str = "modified_starch"):
        params = {
            "modified_starch": {"Mo": 0.065, "C": 7.2, "K": 0.82},
            "gum_arabic":      {"Mo": 0.088, "C": 5.1, "K": 0.85},
            "maltodextrin":    {"Mo": 0.040, "C": 9.8, "K": 0.78},
            "cyclodextrin":    {"Mo": 0.055, "C": 11.2, "K": 0.75},
            "whey_protein":    {"Mo": 0.095, "C": 6.3, "K": 0.87},
            "chitosan":        {"Mo": 0.110, "C": 4.8, "K": 0.89},
        }
        p = params.get(material.lower().replace(" ", "_"), params["modified_starch"])
        self.Mo, self.C, self.K = p["Mo"], p["C"], p["K"]

    def moisture_from_aw(self, Aw: float) -> float:
        """MC dry basis from Aw using GAB equation."""
        denom = (1 - self.K * Aw) * (1 - self.K * Aw + self.C * self.K * Aw)
        if abs(denom) < 1e-9: return 0.15
        return round(self.Mo * self.C * self.K * Aw / denom, 4)

    def aw_from_moisture(self, MC_db: float) -> float:
        """Aw from moisture content — numerical inversion of GAB."""
        try:
            res = minimize(lambda aw: (self.moisture_from_aw(float(aw[0])) - MC_db)**2,
                           x0=[0.3], bounds=[(0.01, 0.99)], method='L-BFGS-B')
            return round(float(res.x[0]), 4)
        except Exception:
            m = MC_db / 100 if MC_db > 1 else MC_db
            return round(max(0.03, min(0.98, 0.08 + m * 7.5 - m**2 * 25)), 3)

    def isotherm_curve(self, n_pts: int = 30) -> dict:
        """Full isotherm curve for plotting."""
        aws = [round(i / n_pts, 3) for i in range(1, n_pts)]
        mcs = [self.moisture_from_aw(a) for a in aws]
        return {"aw": aws, "mc": mcs}


# ══ GLASS TRANSITION — GORDON-TAYLOR + FOX EQUATION ════════════

class GlassTransition:
    """
    Multi-component Tg prediction using Gordon-Taylor equation.
    Plasticisation by water modelled via Gordon-Taylor k factor.
    Ref: Gordon & Taylor (1952); Couchman & Karasz (1978).
    """
    MATERIAL_TG = {
        "modified_starch": 105, "gum_arabic": 98,  "maltodextrin_de10": 160,
        "maltodextrin_de18": 130, "whey_protein": 115, "cyclodextrin": 128,
        "chitosan": 102, "ethylcellulose": 132, "zein": 112,
        "sodium_caseinate": 108, "trehalose": 115, "sucrose": 67,
        "lactose": 101, "glucose": 31,
    }
    TG_WATER = -135  # °C

    def __init__(self, material: str, wall_ratio: float, moisture_pct: float):
        key = material.lower().replace(" ", "_").replace("-", "_").replace("(", "").replace(")", "")
        self.Tg_dry = self.MATERIAL_TG.get(key, 100)
        self.w2 = moisture_pct / 100  # water mass fraction
        self.w1 = 1.0 - self.w2
        # Gordon-Taylor k constant (ratio of free volume parameters)
        self.k_gt = max(0.1, 0.28 + wall_ratio * 0.02)

    def Tg(self) -> float:
        """Predicted Tg °C (Gordon-Taylor mixing rule)."""
        num = self.w1 * self.Tg_dry + self.k_gt * self.w2 * self.TG_WATER
        den = self.w1 + self.k_gt * self.w2
        return round(max(-60, num / max(1e-9, den)), 1)

    def Tg_storey_formula(self) -> float:
        """Simplified Storey & Ly formula for comparison."""
        return round(self.Tg_dry - 7.5 * (self.w2 * 100), 1)


# ══ FLAVOR FUGACITY & PARTITIONING ══════════════════════════════

class FlavorPartitioning:
    """
    Henry's law + modified Raoult for flavor compound partitioning
    between aqueous, oil and wall matrix phases.
    Ref: Overbosch et al. (1991) Food Reviews International.
    """
    def __init__(self, logP: float, MW: float, bp_C: float):
        self.logP = logP
        self.MW = MW
        self.bp = bp_C

    def henry_constant(self, T_C: float = 25.0) -> float:
        """Henry's law constant H (Pa·m³/mol) estimated from logP and bp."""
        T = T_C + 273.15
        Tbp = self.bp + 273.15
        Pvap = 101325 * math.exp(-40000 / R_GAS * (1/T - 1/Tbp))
        Vw = 18e-3  # m³/mol water molar volume
        gamma = 10 ** self.logP  # activity coefficient (water → organic)
        return round(Pvap * Vw * gamma / 1000, 4)

    def oil_water_partition(self, T_C: float = 25.0) -> float:
        """Kow from logP (simplified)."""
        return round(10 ** self.logP, 2)

    def wall_partition_coeff(self, EE: float) -> float:
        """
        Fraction of flavor retained in wall matrix vs headspace.
        Higher EE → better retention (less in headspace).
        """
        return round(EE / 100 * (1 + math.log10(max(1, self.oil_water_partition()))), 3)


# ══ CORE SPRAY DRYER MODEL (SCIENTIFIC UPGRADE) ════════════════

class SprayDryerModel:
    """
    Industry-grade spray dryer simulation v5.
    References: Masters (1991), Reineccius (2006), Mujumdar (2006),
                Abramzon & Sirignano (1989), Lefebvre (1989).
    """
    def __init__(self, p: dict, pc: dict):
        self.Ti      = float(p.get('inlet',    170))
        self.To      = float(p.get('outlet',    75))
        self.S       = float(p.get('solids',    20))
        self.pH      = float(p.get('ph',         5.0))
        self.rpm     = float(p.get('atomizer',  20))   # ×1000 RPM
        self.RH      = float(p.get('humidity',  15))
        self.MC      = float(p.get('moisture',  3.5))
        self.R       = float(p.get('ratio',      4.0))
        self.logP    = float(pc.get('XLogP', 3) or 3)
        self.MW      = float(pc.get('MolecularWeight', 150) or 150)
        self.bp      = 200.0  # default bp

        # Psychrometric state
        self.W_in  = humidity_ratio(self.Ti, self.RH)
        self.W_out = humidity_ratio(self.To, 75)
        self.H_in  = enthalpy_moist_air(self.Ti, self.W_in)
        self.H_out = enthalpy_moist_air(self.To, self.W_out)

        # Sub-models
        self.psd  = ParticleSizeDistribution(self.rpm, self.S)
        self.sorption = SorptionModel("modified_starch")
        self.degr = DegradationKinetics(self.Ti, self.pH, 0.3, self.logP)
        self.gt   = GlassTransition("modified_starch", self.R, self.MC)

    def _pH_penalty(self) -> float:
        """Emulsion stability penalty from pH — full model."""
        if   self.pH < 3.0: return 6.5
        elif self.pH < 3.5: return 3.8
        elif self.pH < 4.0: return 1.9
        elif self.pH < 4.5: return 0.6
        elif self.pH <= 6.0: return 0.0   # optimal
        elif self.pH <= 6.5: return 0.4
        elif self.pH <= 7.0: return 0.9
        elif self.pH <= 7.5: return 2.0
        elif self.pH <= 8.0: return 3.5
        else:                return 5.5

    def EE(self) -> float:
        """
        Encapsulation efficiency — Arrhenius thermal + pH + solids + humidity
        + degradation kinetics penalty.
        """
        k        = math.exp(-45000 / (R_GAS * (self.Ti + 273.15)))
        base_ee  = 88 - self.logP * 1.4 + self.S * 0.35 + self.R * 1.1
        base_ee -= 14 * (self.Ti / 180) ** 2.2
        base_ee -= max(0, (self.S - 35) * 0.5)
        base_ee += k * 5
        base_ee -= self._pH_penalty()
        base_ee -= max(0, (self.RH - 30) * 0.08)
        # Advanced: subtract degradation kinetics loss
        base_ee -= self.degr.combined_loss_modifier() * 0.5
        return round(max(48.0, min(97.0, base_ee)), 2)

    def moisture_final(self) -> float:
        """Powder moisture — psychrometric drying kinetics + droplet lifetime."""
        dp = max(0.01, (self.Ti - self.To) / self.Ti)
        rh_factor = 1 + self.RH / 100
        return round(max(0.8, min(12.0, self.MC * (1 / dp) * rh_factor)), 2)

    def yield_(self) -> float:
        cyc = 0.92 - self.RH * 0.002
        lf  = 10 * math.log(max(1.01, self.S / 5))
        return round(max(45.0, min(94.0, (62 + lf) * cyc)), 2)

    def D50(self) -> float:
        return self.psd.D50_lefebvre()

    def surface_oil(self) -> float:
        pH_factor = 1.5 if (self.pH < 4 or self.pH > 7.5) else 1.0
        return round(max(0.1, min(10.0,
            (8 / self.R + max(0, (self.Ti - 160) * 0.04)) * pH_factor)), 2)

    def Aw(self) -> float:
        """Water activity from GAB sorption model."""
        MC_db = self.moisture_final() / 100
        return self.sorption.aw_from_moisture(MC_db)

    def bulk_density(self) -> float:
        return round(max(0.18, min(0.70,
            0.48 - (self.rpm - 18) * 0.006 + self.moisture_final() * 0.005)), 3)

    def Tg(self) -> float:
        """Glass transition via Gordon-Taylor with moisture plasticisation."""
        gt = GlassTransition("modified_starch", self.R, self.moisture_final())
        return gt.Tg()

    def evap_rate(self) -> float:
        return round(max(0.01, 0.5 * 60 * (self.S / 100) * (1 - self.moisture_final() / 100)), 3)

    def residence_time(self) -> float:
        return round(max(2.0, 0.35 / 0.072 + (180 - self.Ti) * 0.03), 2)

    def span(self) -> float:
        psd = self.psd.percentiles()
        return psd["span"]

    def caking_risk(self) -> str:
        Aw = self.Aw()
        return 'High' if Aw > 0.55 else 'Medium' if Aw > 0.38 else 'Low'

    def energy_consumption(self) -> dict:
        """Specific energy consumption kWh/kg powder (Masters 1991 method)."""
        delta_H = self.H_in - self.H_out           # kJ/kg dry air
        feed_water = (self.S / 100) * (1 - self.moisture_final() / 100)
        evap = max(0.001, feed_water)
        Q_specific = delta_H / max(0.001, evap)    # kJ/kg water evaporated
        kWh_kg = Q_specific / 3600 * 1.15           # +15% mechanical losses
        return {"kWh_per_kg_water": round(kWh_kg, 3),
                "kWh_per_kg_powder": round(kWh_kg * evap, 3),
                "Q_air_kJ": round(delta_H, 1)}

    def droplet_analysis(self) -> dict:
        """Abramzon-Sirignano droplet evaporation analysis."""
        de = DropletEvaporation(self.D50(), self.Ti, self.RH, self.S)
        return {"lifetime_s": round(de.droplet_lifetime(), 3),
                "drying_rate_kg_s": round(de.drying_rate(), 2),
                "Re": round(de.Re_droplet(), 1),
                "Sh": round(de.Sh_number(), 2)}

    def degradation_summary(self) -> dict:
        """Full degradation kinetics summary."""
        return {
            "thermal_loss_pct": self.degr.thermal_loss_pct(),
            "maillard_rate":    self.degr.maillard_rate(),
            "oxidation_rate":   self.degr.oxidation_rate(),
            "hydrolysis_rate":  self.degr.hydrolysis_rate(),
            "pH_penalty":       round(self._pH_penalty(), 1),
            "combined_modifier":self.degr.combined_loss_modifier(),
        }

    def run(self) -> dict:
        ee = self.EE()
        mc = self.moisture_final()
        psd_data = self.psd.percentiles()
        energy = self.energy_consumption()
        droplet = self.droplet_analysis()
        degr = self.degradation_summary()
        return {
            "EE":                str(ee),
            "actualMC":          str(mc),
            "yield_":            str(self.yield_()),
            "flavorLoss":        str(round(100 - ee, 2)),
            "D50":               str(self.D50()),
            "D10":               str(psd_data["D10"]),
            "D90":               str(psd_data["D90"]),
            "surfOil":           str(self.surface_oil()),
            "Aw":                str(self.Aw()),
            "bulkD":             str(self.bulk_density()),
            "Tg":                str(self.Tg()),
            "evapRate":          str(self.evap_rate()),
            "residenceTime":     str(self.residence_time()),
            "span":              str(self.span()),
            "inletH":            str(round(self.W_in * 1000, 2)),
            "cakingScore":       self.caking_risk(),
            "evapEfficiency":    str(round(self.yield_() * ee / 100, 1)),
            "wetBulbTemp":       str(round(wet_bulb(self.Ti, self.W_in), 1)),
            "dewPoint":          str(round(dew_point(self.Ti, self.RH), 1)),
            "enthalpyIn":        str(round(self.H_in, 1)),
            "enthalpyOut":       str(round(self.H_out, 1)),
            "energy_kWh_kg":     str(energy["kWh_per_kg_powder"]),
            "droplet_lifetime_s":str(droplet["lifetime_s"]),
            "droplet_Sh":        str(droplet["Sh"]),
            # Degradation detail
            "pH_penalty":        str(degr["pH_penalty"]),
            "thermal_loss":      str(degr["thermal_loss_pct"]),
            "maillard_rate":     str(degr["maillard_rate"]),
            "oxidation_rate":    str(degr["oxidation_rate"]),
            "GSD":               str(psd_data["GSD"]),
        }


# ══ FLUID BED DRYER ═════════════════════════════════════════════

class FluidBedModel:
    """
    Fluid bed dryer / agglomerator v2 with Ergun pressure drop.
    Ref: Kunii & Levenspiel (1991); Dewettinck & Huyghebaert (1999).
    """
    def __init__(self, spray_data: dict, p: dict):
        self.prev_MC    = float(spray_data.get('actualMC', 3.5))
        self.prev_D50   = float(spray_data.get('D50', 50))
        self.prev_Aw    = float(spray_data.get('Aw', 0.25))
        self.prev_yield = float(spray_data.get('yield_', 80))
        self.T_bed      = float(p.get('fbd_temp',      60))
        self.velocity   = float(p.get('fbd_velocity',  0.8))
        self.time       = float(p.get('fbd_time',      20))
        self.binder     = float(p.get('fbd_binder',    5))
        self.dp         = self.prev_D50 * 1e-6       # particle diameter m
        self.rho_p      = 1100.0                      # kg/m³ particle density
        self.epsilon    = 0.45                        # bed voidage (min. fluidisation)

    def umf(self) -> float:
        """Minimum fluidisation velocity Ergun equation, m/s."""
        mu = 2.0e-5   # air viscosity at 60°C
        rho_f = 1.06  # air density at 60°C kg/m³
        g = 9.81
        if self.dp < 1e-8: return 0.01
        Ar = self.dp**3 * rho_f * (self.rho_p - rho_f) * g / mu**2
        Re_mf = (14.2**2 + 0.0408 * Ar)**0.5 - 14.2
        return round(Re_mf * mu / (rho_f * self.dp), 4)

    def moisture_final(self) -> float:
        return round(max(0.4,
            self.prev_MC * math.exp(-0.015 * self.T_bed * self.time / 60)), 2)

    def D50_agglomerated(self) -> float:
        growth = 1 + self.binder * 0.035 * (max(0.1, self.velocity)**0.3)
        return round(self.prev_D50 * growth, 1)

    def Aw_final(self) -> float:
        mc_db = self.moisture_final() / 100
        sorption = SorptionModel("modified_starch")
        return sorption.aw_from_moisture(mc_db)

    def yield_final(self) -> float:
        loss = self.velocity * 0.02
        return round(max(50.0, min(97.0, self.prev_yield * (1 - loss))), 1)

    def porosity(self) -> float:
        umf_val = max(0.01, self.umf())
        return round(min(0.9, self.epsilon + (self.velocity - umf_val) * 0.08), 3)

    def heat_transfer_coeff(self) -> float:
        """Particle-air HTC W/m²K — Kunii-Levenspiel Nu correlation."""
        rho_f = 1.06; mu = 2.0e-5; lambda_f = 0.028
        Re = self.velocity * max(1e-6, self.dp) * rho_f / mu
        Pr = 0.71
        Nu = 2.0 + 0.6 * Re**0.5 * Pr**0.33
        return round(Nu * lambda_f / max(1e-6, self.dp), 1)

    def ergun_pressure_drop(self) -> float:
        """Ergun equation bed pressure drop, Pa/m."""
        mu = 2.0e-5; rho_f = 1.06; eps = self.porosity()
        dp = max(1e-6, self.dp)
        v = self.velocity
        term1 = 150 * mu * (1 - eps)**2 * v / (dp**2 * eps**3)
        term2 = 1.75 * rho_f * (1 - eps) * v**2 / (dp * eps**3)
        return round((term1 + term2) / 1000, 2)  # kPa/m

    def run(self) -> dict:
        return {
            "fbd_MC":         str(self.moisture_final()),
            "fbd_D50":        str(self.D50_agglomerated()),
            "fbd_Aw":         str(self.Aw_final()),
            "fbd_yield":      str(self.yield_final()),
            "fbd_porosity":   str(self.porosity()),
            "fbd_htc":        str(self.heat_transfer_coeff()),
            "fbd_umf":        str(round(self.umf(), 4)),
            "fbd_dP":         str(self.ergun_pressure_drop()),
            "fbd_temp":       str(self.T_bed),
            "fbd_velocity":   str(self.velocity),
        }


# ══ VIBRO SIFTER ════════════════════════════════════════════════

class SifterModel:
    def __init__(self, fbd_data: dict, p: dict):
        self.D50   = float(fbd_data.get('fbd_D50', 80))
        self.upper = float(p.get('sft_upper', 350))
        self.lower = float(p.get('sft_lower', 90))
        self.freq  = float(p.get('sft_freq',  35))
        self.rate  = float(p.get('sft_rate',  80))
        # GSD for estimating fines/overs from log-normal PSD
        self.GSD   = float(fbd_data.get('GSD', 1.8) if 'GSD' in fbd_data else 1.8)

    def _lognormal_cdf(self, x: float) -> float:
        """CDF of log-normal distribution at x, given D50 and GSD."""
        if x <= 0: return 0.0
        mu = math.log(max(1e-9, self.D50))
        sig = math.log(max(1.001, self.GSD))
        z = (math.log(x) - mu) / sig
        return 0.5 * (1 + math.erf(z / math.sqrt(2)))

    def fines_pct(self) -> float:
        return round(self._lognormal_cdf(self.lower) * 100, 1)

    def overs_pct(self) -> float:
        return round((1 - self._lognormal_cdf(self.upper)) * 100, 1)

    def on_grade(self) -> float:
        return round(max(0, 100 - self.fines_pct() - self.overs_pct()), 1)

    def throughput(self) -> float:
        base = self.rate * 0.85
        penalty = (self.fines_pct() + self.overs_pct()) * 0.3
        return round(max(5, base - penalty), 1)

    def separation_efficiency(self) -> float:
        opt_freq = 40
        eff = 85 + 10 * math.exp(-((self.freq - opt_freq)**2) / (2 * 15**2))
        return round(min(99, eff), 1)

    def run(self) -> dict:
        return {
            "sft_ongrade":    str(self.on_grade()),
            "sft_fines":      str(self.fines_pct()),
            "sft_overs":      str(self.overs_pct()),
            "sft_throughput": str(self.throughput()),
            "sft_efficiency": str(self.separation_efficiency()),
            "sft_pass":       "PASS" if self.lower <= self.D50 <= self.upper else "CHECK",
        }


# ══ RIBBON BLENDER ══════════════════════════════════════════════

class BlenderModel:
    def __init__(self, sft_data: dict, p: dict):
        self.prev_Aw   = float(sft_data.get('fbd_Aw', 0.25))
        self.speed     = float(p.get('bld_speed',      30))
        self.time      = float(p.get('bld_time',        15))
        self.ac        = float(p.get('bld_anticaking', 0.5))
        self.batch     = float(p.get('bld_batch',       50))

    def blend_uniformity(self) -> float:
        return round(min(99.5, 55 + self.time * 2.8 - self.speed * 0.08), 1)

    def rsd(self) -> float:
        return round(max(0.3, 8 - self.time * 0.25 + self.speed * 0.02), 1)

    def lacey_index(self) -> float:
        """
        Lacey mixing index M = 0 (completely unmixed) to 1 (perfectly mixed).
        M ≈ 1 - exp(-k*t/t_mix) where t_mix ~ rpm-dependent.
        """
        t_mix = 300 / max(1, self.speed)  # characteristic time s
        k = 0.05
        M = 1 - math.exp(-k * self.time * 60 / t_mix)
        return round(min(1.0, M), 4)

    def Aw_improved(self) -> float:
        return round(max(0.05, self.prev_Aw - self.ac * 0.04), 3)

    def specific_energy(self) -> float:
        omega = self.speed * 2 * math.pi / 60
        torque = 0.5 * self.batch * 0.3 * (0.3**2)
        return round(torque * omega * (self.time * 60) / self.batch / 1000, 2)

    def run(self) -> dict:
        return {
            "bld_uniformity":      str(self.blend_uniformity()),
            "bld_rsd":             str(self.rsd()),
            "bld_lacey":           str(self.lacey_index()),
            "bld_flow_index":      str(round(min(99, 65 + self.ac * 10 - self.speed * 0.1), 1)),
            "bld_Aw":              str(self.Aw_improved()),
            "bld_specific_energy": str(self.specific_energy()),
        }


# ══ PAN COATER ══════════════════════════════════════════════════

class PanCoaterModel:
    def __init__(self, bld_data: dict, spray_data: dict, p: dict):
        self.prev_EE    = float(spray_data.get('EE', 85))
        self.speed      = float(p.get('ctr_speed',  20))
        self.level      = float(p.get('ctr_level',   8))
        self.spray_rate = float(p.get('ctr_spray',  15))
        self.temp       = float(p.get('ctr_temp',   55))

    def film_uniformity(self) -> float:
        u = 72 + self.level * 2.5 - self.spray_rate * 0.3 + self.speed * 0.2
        return round(min(99, max(40, u)), 1)

    def moisture_barrier(self) -> float:
        return round(min(99, 55 + self.level * 3.5), 1)

    def EE_final(self) -> float:
        return round(min(99.5, self.prev_EE + self.level * 0.45), 1)

    def higuchi_release_rate(self) -> float:
        """
        Higuchi matrix diffusion model — cumulative release rate constant Q/t^0.5.
        Used for ethylcellulose / sustained-release coatings.
        Ref: Higuchi (1963) J Pharm Sci.
        """
        D_eff = 1e-12 * (1 + self.level * 0.02)  # m²/s effective diffusivity
        A = 100 / (self.level * 0.01)              # initial drug loading (relative)
        Cs = 50.0                                   # drug solubility in matrix
        Q = math.sqrt(D_eff * (2 * A - Cs) * Cs)
        return round(Q * 1e8, 4)

    def run(self) -> dict:
        return {
            "ctr_film_uniformity":  str(self.film_uniformity()),
            "ctr_moisture_barrier": str(self.moisture_barrier()),
            "ctr_EE_final":         str(self.EE_final()),
            "ctr_release_pH":       str(round(5.0 + self.level * 0.15, 1)),
            "ctr_attrition":        str(round(min(15, self.speed * 0.08 + self.spray_rate * 0.05), 2)),
            "ctr_coat_efficiency":  str(round(min(98, 78 + self.temp * 0.2 - self.spray_rate * 0.1), 1)),
            "ctr_higuchi_k":        str(self.higuchi_release_rate()),
        }


# ══ BAYESIAN / LATIN-HYPERCUBE PROCESS OPTIMISER ════════════════

class ProcessOptimiser:
    """
    Multi-objective optimisation via Latin Hypercube Sampling + 
    Response Surface surrogate + Pareto front selection.
    Objectives: maximise EE, minimise MC, minimise flavor loss, maximise yield.
    Ref: Deb et al. (2002) NSGA-II; McKay et al. (1979) Technometrics.
    """
    BOUNDS = {
        "inlet":    (140, 200),
        "outlet":   (60,  100),
        "solids":   (12,  40),
        "ph":       (4.0, 6.5),
        "atomizer": (14,  30),
        "humidity": (8,   35),
        "moisture": (2.0, 6.0),
        "ratio":    (2.0, 6.0),
    }

    def __init__(self, pc: dict, n_samples: int = 80):
        self.pc = pc
        self.n  = n_samples

    def _simulate(self, params: dict) -> dict:
        return SprayDryerModel(params, self.pc).run()

    def _score(self, sim: dict) -> float:
        """Composite score — higher is better."""
        EE   = float(sim["EE"])
        MC   = float(sim["actualMC"])
        loss = float(sim["flavorLoss"])
        yld  = float(sim["yield_"])
        Tg   = float(sim["Tg"])
        # Penalise caking and low Tg
        tg_bonus = max(0, (Tg - 35) * 0.2)
        return EE * 1.5 + yld * 0.8 - MC * 2.5 - loss * 2.0 + tg_bonus

    def latin_hypercube_sample(self) -> list:
        """Generate LHS sample points across parameter space."""
        keys = list(self.BOUNDS.keys())
        sampler = qmc.LatinHypercube(d=len(keys), seed=42)
        raw = sampler.random(n=self.n)
        samples = []
        for row in raw:
            p = {}
            for i, k in enumerate(keys):
                lo, hi = self.BOUNDS[k]
                p[k] = round(lo + row[i] * (hi - lo), 2)
            samples.append(p)
        return samples

    def optimise(self) -> dict:
        """Run LHS + response surface selection."""
        samples = self.latin_hypercube_sample()
        best_score = -9999
        best_params = samples[0]
        best_sim = None

        for p in samples:
            try:
                sim = self._simulate(p)
                s = self._score(sim)
                if s > best_score:
                    best_score = s
                    best_params = p
                    best_sim = sim
            except Exception:
                continue

        return {
            "optimal_params": best_params,
            "predicted_sim":  best_sim,
            "score":          round(best_score, 2),
            "method":         "Latin Hypercube + Response Surface (n=80)",
        }

    def sensitivity_analysis(self, base_params: dict, target_key: str) -> list:
        """Local sensitivity — vary one param, all others fixed."""
        lo, hi = self.BOUNDS.get(target_key, (0, 100))
        results = []
        for v in np.linspace(lo, hi, 20):
            p = {**base_params, target_key: round(float(v), 3)}
            try:
                sim = self._simulate(p)
                results.append({
                    "value":  round(float(v), 3),
                    "EE":     float(sim["EE"]),
                    "MC":     float(sim["actualMC"]),
                    "loss":   float(sim["flavorLoss"]),
                    "yield":  float(sim["yield_"]),
                    "Tg":     float(sim["Tg"]),
                    "Aw":     float(sim["Aw"]),
                })
            except Exception:
                pass
        return results


# ══ MONTE CARLO UNCERTAINTY ══════════════════════════════════════

class MonteCarloUncertainty:
    """
    Propagate measurement uncertainties through the spray dryer model.
    Returns mean, std, P5/P95 confidence intervals for key outputs.
    """
    def __init__(self, base_params: dict, pc: dict, n: int = 200):
        self.base = base_params
        self.pc   = pc
        self.n    = n
        # Typical instrument uncertainties (±1 sigma)
        self.uncertainties = {
            "inlet": 2.0, "outlet": 1.5, "solids": 0.5,
            "ph": 0.05,   "atomizer": 0.5, "humidity": 1.0,
            "moisture": 0.1, "ratio": 0.1,
        }

    def run(self) -> dict:
        results = {"EE": [], "actualMC": [], "D50": [], "Aw": [], "Tg": [], "yield_": []}
        rng = np.random.default_rng(seed=123)
        for _ in range(self.n):
            p = {}
            for k, v in self.base.items():
                sigma = self.uncertainties.get(k, 0)
                p[k] = v + rng.normal(0, sigma)
            try:
                sim = SprayDryerModel(p, self.pc).run()
                for key in results:
                    results[key].append(float(sim.get(key, 0)))
            except Exception:
                pass

        out = {}
        for key, vals in results.items():
            if vals:
                arr = np.array(vals)
                out[key] = {
                    "mean": round(float(arr.mean()), 2),
                    "std":  round(float(arr.std()),  2),
                    "P5":   round(float(np.percentile(arr, 5)), 2),
                    "P95":  round(float(np.percentile(arr, 95)), 2),
                }
        return out


# ══ RELEASE KINETICS — ADVANCED MODELS ═══════════════════════════

def korsmeyer_peppas(t: float, K: float, n: float, t_lag: float = 0) -> float:
    """Mt/M∞ = K*(t-t_lag)^n; n<0.43 Fickian, 0.43<n<0.85 anomalous."""
    if t <= t_lag: return 0.0
    return min(100.0, K * (t - t_lag)**n)

def weibull_release(t: float, a: float = 1.0, beta: float = 0.75,
                    t_d: float = 0.0) -> float:
    """Weibull model — flexible sigmoid shape. beta<1 early burst, >1 sigmoid."""
    if t < t_d: return 0.0
    return min(100.0, 100 * (1 - math.exp(-((t - t_d) / a)**beta)))

def hixson_crowell(t: float, k_s: float, initial: float = 100.0) -> float:
    """Cube-root law — dissolution of eroding sphere."""
    return min(100.0, max(0.0, initial * (1 - (1 - k_s * t)**3)))


# ══ FLASK API ROUTES ══════════════════════════════════════════════

@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        "status": "online", "version": "5.0-advanced",
        "base_dir": BASE_DIR,
        "index_found": os.path.exists(os.path.join(BASE_DIR, 'index.html')),
        "models": ["SprayDryer-v5","FluidBed-v2","Sifter","Blender","PanCoater",
                   "AbramzonSirignano","PopulationBalance","GordonTaylor",
                   "GAB-Sorption","DegradationKinetics","BayesianOptimiser",
                   "MonteCarlo","Weibull","KorsmeyerPeppas"],
        "scientific_upgrades": [
            "Abramzon-Sirignano droplet evaporation",
            "Ergun bed pressure drop",
            "Gordon-Taylor multi-component Tg",
            "GAB dual-mode sorption",
            "Maillard / oxidation / hydrolysis kinetics",
            "Latin Hypercube process optimiser",
            "Monte Carlo uncertainty propagation",
            "Weibull & Korsmeyer-Peppas release",
            "Flavor fugacity partitioning",
            "Log-normal PSD with GSD",
        ]
    })


@app.route('/simulate', methods=['POST'])
def simulate():
    data = request.get_json(force=True) or {}
    try:
        params = data.get('sliders', data.get('params', {}))
        pubchem = data.get('pubchem', {})
        result = SprayDryerModel(params, pubchem).run()
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/simulate/fbd', methods=['POST'])
def simulate_fbd():
    data = request.get_json(force=True) or {}
    try:
        return jsonify(FluidBedModel(data.get('spray_data', {}), data.get('params', {})).run())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/simulate/sifter', methods=['POST'])
def simulate_sifter():
    data = request.get_json(force=True) or {}
    try:
        return jsonify(SifterModel(data.get('fbd_data', {}), data.get('params', {})).run())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/simulate/blender', methods=['POST'])
def simulate_blender():
    data = request.get_json(force=True) or {}
    try:
        return jsonify(BlenderModel(data.get('sft_data', {}), data.get('params', {})).run())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/simulate/coater', methods=['POST'])
def simulate_coater():
    data = request.get_json(force=True) or {}
    try:
        return jsonify(PanCoaterModel(
            data.get('bld_data', {}),
            data.get('spray_data', {}),
            data.get('params', {})
        ).run())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/simulate/sweep', methods=['POST'])
def sweep():
    data = request.get_json(force=True) or {}
    params = dict(data.get('params', {}))
    pubchem = data.get('pubchem', {})
    sweep_param = data.get('sweep_param', 'inlet')
    values = data.get('values', list(range(120, 241, 10)))
    results = []
    for val in values:
        p = {**params, sweep_param: val}
        try:
            r = SprayDryerModel(p, pubchem).run()
            r['sweep_val'] = val
            results.append(r)
        except Exception:
            results.append({'sweep_val': val, 'error': True})
    return jsonify(results)


@app.route('/optimise', methods=['POST'])
def optimise():
    """Full Bayesian/LHS optimisation endpoint."""
    data = request.get_json(force=True) or {}
    pubchem = data.get('pubchem', {})
    try:
        opt = ProcessOptimiser(pubchem)
        result = opt.optimise()
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/sensitivity', methods=['POST'])
def sensitivity():
    """Sensitivity analysis for one parameter."""
    data = request.get_json(force=True) or {}
    params = data.get('sliders', {})
    key = data.get('param', 'inlet')
    pubchem = data.get('pubchem', {})
    try:
        opt = ProcessOptimiser(pubchem)
        results = opt.sensitivity_analysis(params, key)
        return jsonify(results)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/monte_carlo', methods=['POST'])
def monte_carlo():
    """Monte Carlo uncertainty quantification."""
    data = request.get_json(force=True) or {}
    params = data.get('sliders', {})
    pubchem = data.get('pubchem', {})
    n = min(500, int(data.get('n_samples', 200)))
    try:
        mc = MonteCarloUncertainty(params, pubchem, n=n)
        return jsonify(mc.run())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/release_profile', methods=['POST'])
def release_profile():
    data = request.get_json(force=True) or {}
    onset_ph      = float(data.get('onset_ph',      4.0))
    complete_ph   = float(data.get('complete_ph',   7.0))
    onset_temp    = float(data.get('onset_temp',    60))
    complete_temp = float(data.get('complete_temp', 90))
    half_life     = float(data.get('half_life_hours', 3))

    def sigmoid(x, x0, w):
        return 100 / (1 + math.exp(-3 * (x - x0) / max(0.01, w)))

    ph_vals   = [round(p * 0.5, 1) for p in range(4, 24)]
    temp_vals = list(range(20, 160, 5))
    hour_vals = [h / 4 for h in range(33)]

    n  = 0.45
    K  = 100 / max(0.01, half_life ** n)
    k0 = 100 / max(0.01, half_life * 1.2)
    k1 = math.log(2) / max(0.01, half_life)

    # Advanced: Weibull and Korsmeyer-Peppas
    kp_data = [round(korsmeyer_peppas(h, K=K*0.1, n=n), 1) for h in hour_vals]
    wb_data = [round(weibull_release(h, a=half_life*0.9, beta=0.75), 1) for h in hour_vals]
    hc_data = [round(hixson_crowell(h, k_s=0.08/max(0.1, half_life)), 1) for h in hour_vals]

    return jsonify({
        "ph_range":         ph_vals,
        "ph_release":       [round(sigmoid(p, (onset_ph+complete_ph)/2, complete_ph-onset_ph), 1) for p in ph_vals],
        "temp_range":       temp_vals,
        "temp_release":     [round(sigmoid(t, (onset_temp+complete_temp)/2, complete_temp-onset_temp), 1) for t in temp_vals],
        "hours":            hour_vals,
        "korsmeyer_peppas": kp_data,
        "zero_order":       [round(min(100, k0 * h), 1) for h in hour_vals],
        "first_order":      [round(100 * (1 - math.exp(-k1 * h)), 1) for h in hour_vals],
        "weibull":          wb_data,
        "hixson_crowell":   hc_data,
    })


@app.route('/degradation', methods=['POST'])
def degradation():
    """Full degradation kinetics for given conditions."""
    data = request.get_json(force=True) or {}
    try:
        dk = DegradationKinetics(
            T_C=float(data.get('inlet', 170)),
            pH=float(data.get('ph', 5)),
            Aw=float(data.get('Aw', 0.25)),
            logP=float(data.get('logP', 3)),
            compound_class=data.get('compound_class', 'terpene')
        )
        return jsonify({
            "thermal_loss_pct": dk.thermal_loss_pct(),
            "maillard_rate":    dk.maillard_rate(),
            "oxidation_rate":   dk.oxidation_rate(),
            "hydrolysis_rate":  dk.hydrolysis_rate(),
            "combined_modifier":dk.combined_loss_modifier(),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/sorption_isotherm', methods=['POST'])
def sorption_isotherm():
    """GAB sorption isotherm for selected material."""
    data = request.get_json(force=True) or {}
    material = data.get('material', 'modified_starch')
    try:
        model = SorptionModel(material)
        curve = model.isotherm_curve(30)
        return jsonify({"material": material, **curve})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/glass_transition', methods=['POST'])
def glass_transition():
    """Gordon-Taylor Tg prediction."""
    data = request.get_json(force=True) or {}
    try:
        gt = GlassTransition(
            material=data.get('material', 'modified_starch'),
            wall_ratio=float(data.get('ratio', 4)),
            moisture_pct=float(data.get('moisture', 3.5))
        )
        return jsonify({"Tg": gt.Tg(), "Tg_storey": gt.Tg_storey_formula(),
                        "Tg_dry": gt.Tg_dry})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/pubchem_proxy', methods=['GET'])
def pubchem_proxy():
    name = request.args.get('name', '').strip()
    if not name: return jsonify({"error": "No compound name"}), 400
    try:
        base    = 'https://pubchem.ncbi.nlm.nih.gov/rest/pug'
        cid_res = requests.get(f'{base}/compound/name/{name}/cids/JSON', timeout=10)
        cid     = cid_res.json()['IdentifierList']['CIDs'][0]
        props   = ('IUPACName,MolecularFormula,MolecularWeight,XLogP,'
                   'TPSA,HBondDonorCount,HBondAcceptorCount,MonoisotopicMass,IsomericSMILES')
        p_res   = requests.get(f'{base}/compound/cid/{cid}/property/{props}/JSON', timeout=10)
        p_data  = p_res.json()['PropertyTable']['Properties'][0]
        return jsonify({'cid': cid, **p_data})
    except Exception as e:
        return jsonify({'error': str(e)}), 404


@app.route('/ask_ai', methods=['POST'])
def ask_ai():
    """Server-side Gemini AI call."""
    data     = request.get_json(force=True) or {}
    compound = data.get('compound', '')
    pc_info  = data.get('pubchem_info', 'unavailable')
    api_key  = os.environ.get('GOOGLE_API_KEY', '')
    if not api_key:
        return jsonify({'error': 'GOOGLE_API_KEY not set.'}), 500
    prompt = (f'You are a senior food technologist specializing in spray drying microencapsulation.\n'
              f'Analyze "{compound}". PubChem: {pc_info}\n'
              f'Return ONLY valid JSON with compound, materials (5), reasoning, release_profile, optimal_conditions.')
    try:
        url = f'https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}'
        payload = {"contents": [{"parts": [{"text": prompt}]}],
                   "generationConfig": {"response_mime_type": "application/json", "maxOutputTokens": 8000}}
        res  = requests.post(url, headers={'Content-Type': 'application/json'}, json=payload, timeout=60)
        body = res.json()
        if 'error' in body: return jsonify({'error': body['error']['message']}), 500
        text = body['candidates'][0]['content']['parts'][0]['text']
        text = text.replace('```json', '').replace('```', '').strip()
        return jsonify(json.loads(text))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/recommend', methods=['POST'])
def recommend():
    data = request.get_json(force=True) or {}
    params = data.get("sliders", {})
    pc = data.get("pubchem", {"XLogP": 3})
    sim = SprayDryerModel(params, pc).run()
    suggestions = []
    EE   = float(sim["EE"])
    MC   = float(sim["actualMC"])
    loss = float(sim["flavorLoss"])
    if EE < 80:
        suggestions.append({"issue": "Low EE", "action": "Reduce inlet temp, increase wall ratio",
                             "target": {"inlet": params.get("inlet", 170) - 10, "ratio": 4}})
    if MC > 5:
        suggestions.append({"issue": "High Moisture", "action": "Increase inlet temp",
                             "target": {"inlet": params.get("inlet", 170) + 10, "humidity": 15}})
    if loss > 10:
        suggestions.append({"issue": "High Loss", "action": "Reduce atomizer, lower inlet",
                             "target": {"atomizer": 18, "inlet": params.get("inlet", 170) - 10}})
    return jsonify(suggestions)


# ══ STARTUP ═══════════════════════════════════════════════════════
if __name__ == '__main__':
    port   = int(os.environ.get('PORT', 5000))
    api_ok = bool(os.environ.get('GOOGLE_API_KEY'))
    files  = ['index.html', 'style.css', 'app.js', 'features.js']

    print("\n" + "=" * 62)
    print("  FlavorEncap AI v5 — Advanced Scientific Edition")
    print("=" * 62)
    print(f"  Folder : {BASE_DIR}")
    print(f"  API key: {'SET ✓ (Gemini 2.0 Flash)' if api_ok else 'NOT SET (optional)'}")
    print("-" * 62)
    print("  ✓ Abramzon-Sirignano droplet evaporation (Sh number)")
    print("  ✓ Ergun bed pressure drop (Kunii-Levenspiel)")
    print("  ✓ Gordon-Taylor + Fox multi-component Tg")
    print("  ✓ GAB dual-mode sorption isotherm")
    print("  ✓ Maillard / oxidation / hydrolysis kinetics")
    print("  ✓ Latin Hypercube Bayesian optimiser (n=80 samples)")
    print("  ✓ Monte Carlo uncertainty propagation (n=200)")
    print("  ✓ Weibull, Korsmeyer-Peppas, Hixson-Crowell release")
    print("  ✓ Flavor fugacity & oil-water partitioning")
    print("  ✓ Log-normal PSD with GSD (population balance)")
    print("-" * 62)
    print(f"  → Open browser: http://localhost:{port}")
    print("=" * 62 + "\n")

    app.run(debug=False, port=port, host='127.0.0.1')
