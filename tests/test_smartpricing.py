"""Rodar (na raiz da ponte): python -m unittest tests.test_smartpricing -v

Os números vêm dos exemplos da documentação da Francilene (SmartPricing-Reforma-Legal,
docs/DOCUMENTACAO.md §7): produto 13644, Peixe Bonito e a tabela do final 9.
"""
import unittest
from decimal import Decimal

from app.core.smartpricing import analise_smartpricing, final9, simular


def linha(**kw):
    base = {
        "id_loja": "1", "loja": "LOJA 1", "id_produto": "1", "descricaocompleta": "PRODUTO",
        "ncm": "12345678", "precovenda": "0", "aliq_pis": "0", "aliq_cofins": "0",
        "aliq_icms_efetiva": "0", "cst_icms": "00", "reducao_ibscbs": "0",
        "custocomimposto": "0", "custosemimposto": "0", "cclasstrib": "000001",
        "cst_ibscbs": "000", "origem_cclass": "PADRAO",
        "mercadologico1": "501", "mercadologico2": "1", "mercadologico3": "0", "mercadologico4": "0",
        "merc1": "MERCEARIA", "merc2": "DOCES", "merc3": "", "merc4": "",
    }
    base.update(kw)
    return base


class Final9(unittest.TestCase):
    def test_tabela_da_documentacao(self):
        for calculado, esperado in [("14.48", "14.49"), ("20.00", "20.09"), ("14.49", "14.49"),
                                    ("44.42", "44.49"), ("2.11", "2.19")]:
            self.assertEqual(final9(Decimal(calculado)), Decimal(esperado), calculado)

    def test_preco_zero_continua_zero(self):
        self.assertEqual(final9(Decimal(0)), Decimal(0))


class Simulacao(unittest.TestCase):
    def test_produto_13644_salgadinho(self):
        r = simular(linha(precovenda="2.19", aliq_pis="1.65", aliq_cofins="7.6", aliq_icms_efetiva="20"))
        self.assertEqual(r["preco_calculado"], 2.11)
        self.assertEqual(r["preco_sugerido"], 2.19)
        self.assertFalse(r["ajuste_margem"])
        self.assertEqual(r["aliq_cbs"], 9.2)

    def test_peixe_bonito_trava_de_margem(self):
        r = simular(linha(precovenda="12.99", custocomimposto="9.00", custosemimposto="9.00"))
        self.assertEqual(r["preco_calculado"], 14.19)
        self.assertTrue(r["ajuste_margem"])
        self.assertEqual(r["preco_sugerido"], 14.79)
        self.assertEqual(r["preco_sem_recomposicao"], 14.19)   # o que seria sem a trava

    def test_sem_custo_nao_tem_trava(self):
        r = simular(linha(precovenda="12.99"))
        self.assertFalse(r["ajuste_margem"])
        self.assertEqual(r["preco_sugerido"], 14.19)

    def test_margem_ja_negativa_nao_tem_trava(self):
        r = simular(linha(precovenda="10.00", custocomimposto="12", custosemimposto="12"))
        self.assertFalse(r["ajuste_margem"])

    def test_reducao_do_cclasstrib(self):
        self.assertEqual(simular(linha(precovenda="10", reducao_ibscbs="60"))["aliq_cbs"], 3.68)
        self.assertEqual(simular(linha(precovenda="10", reducao_ibscbs="100"))["aliq_cbs"], 0.0)
        # redução total: só o final 9 mexe no preço
        self.assertEqual(simular(linha(precovenda="10", reducao_ibscbs="100"))["preco_sugerido"], 10.09)

    def test_sem_icms_de_consumidor_e_sinalizado(self):
        self.assertTrue(simular(linha(precovenda="10", cst_icms=None, aliq_icms_efetiva=None))["sem_icms"])
        self.assertTrue(simular(linha(precovenda="10", cst_icms=""))["sem_icms"])
        self.assertFalse(simular(linha(precovenda="10"))["sem_icms"])

    def test_valores_vazios_ou_invalidos_viram_zero(self):
        r = simular(linha(precovenda="10,50", aliq_pis="", aliq_cofins=None, aliq_icms_efetiva="abc"))
        self.assertEqual(r["preco_atual"], 10.5)
        self.assertEqual(r["aliq_icms"], 0.0)

    def test_mercadologico_encadeado(self):
        # como no projeto original: códigos encadeados nível a nível (níveis abaixo ficam com 0)
        self.assertEqual(simular(linha(precovenda="10"))["merc"], ["501", "501.1", "501.1.0", "501.1.0.0"])
        self.assertEqual(simular(linha(precovenda="10", mercadologico2=""))["merc"], ["501"])


class Analise(unittest.TestCase):
    def test_resumo_lojas_e_mercadologico(self):
        a = analise_smartpricing([
            linha(id_loja="2", loja="LOJA 2", id_produto="1", precovenda="12.99", custocomimposto="9", custosemimposto="9"),
            linha(id_loja="1", loja="LOJA 1", id_produto="2", precovenda="5", cst_icms=None),
        ])
        self.assertEqual(a["total"], 2)
        self.assertEqual([l["id"] for l in a["lojas"]], ["1", "2"])
        self.assertEqual(a["com_trava_margem"], 1)
        self.assertEqual(a["sem_icms"], 1)
        self.assertEqual(a["sem_custo"], 1)
        self.assertEqual(a["mercadologico"]["501"], "MERCEARIA")
        self.assertEqual(a["mercadologico"]["501.1"], "DOCES")


if __name__ == "__main__":
    unittest.main()
