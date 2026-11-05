"""Тесты разбора команд."""

from __future__ import annotations

import unittest

from aidota import commands
from tests.helpers import catalog


class CommandParsingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = catalog()

    def parse(self, text, active_trainer=None):
        return commands.parse_command(text, self.catalog, active_trainer=active_trainer)

    def test_items_with_hero(self):
        intent = self.parse("что купить на антимаге")
        self.assertEqual(intent.kind, commands.ITEMS)
        self.assertEqual(intent.hero_name, "Anti-Mage")

    def test_items_without_hero(self):
        intent = self.parse("какие предметы мне собрать")
        self.assertEqual(intent.kind, commands.ITEMS)
        self.assertIsNone(intent.hero)

    def test_items_alias_case(self):
        intent = self.parse("сборка врки")
        self.assertEqual(intent.kind, commands.ITEMS)
        self.assertEqual(intent.hero_name, "Windranger")

    def test_advice(self):
        intent = self.parse("как играть за пуджа")
        self.assertEqual(intent.kind, commands.ADVICE)
        self.assertEqual(intent.hero_name, "Pudge")

    def test_meta(self):
        intent = self.parse("мета")
        self.assertEqual(intent.kind, commands.META)
        self.assertIsNone(intent.hero)

    def test_meta_tokens_do_not_become_hero(self):
        intent = self.parse("топ героев по винрейту")
        self.assertEqual(intent.kind, commands.META)

    def test_draft(self):
        self.assertEqual(self.parse("анализ драфта").kind, commands.DRAFT)
        self.assertEqual(self.parse("кого банить").kind, commands.DRAFT)
        self.assertEqual(self.parse("баны").kind, commands.DRAFT)

    def test_status(self):
        self.assertEqual(self.parse("что по игре").kind, commands.STATUS)
        self.assertEqual(self.parse("счёт").kind, commands.STATUS)
        self.assertEqual(self.parse("сколько времени").kind, commands.STATUS)

    def test_exit(self):
        self.assertEqual(self.parse("выход").kind, commands.EXIT)
        self.assertEqual(self.parse("пока").kind, commands.EXIT)
        self.assertEqual(self.parse("отключись").kind, commands.EXIT)

    def test_overlay_commands_are_not_exit(self):
        self.assertEqual(self.parse("покажи окно").kind, commands.OVERLAY_SHOW)
        self.assertEqual(self.parse("скрой окно").kind, commands.OVERLAY_HIDE)

    def test_silence_and_voice(self):
        self.assertEqual(self.parse("замолчи").kind, commands.SILENCE)
        self.assertEqual(self.parse("стоп").kind, commands.SILENCE)
        self.assertEqual(self.parse("говори").kind, commands.VOICE)

    def test_repeat(self):
        self.assertEqual(self.parse("повтори").kind, commands.REPEAT)
        self.assertEqual(self.parse("ещё раз").kind, commands.REPEAT)

    def test_help(self):
        self.assertEqual(self.parse("помощь").kind, commands.HELP)
        self.assertEqual(self.parse("что умеешь").kind, commands.HELP)

    def test_select_hero_only(self):
        self.assertEqual(self.parse("пудж").kind, commands.SELECT_HERO)
        intent = self.parse("я играю за пуджа")
        self.assertEqual(intent.kind, commands.SELECT_HERO)
        self.assertEqual(intent.hero_name, "Pudge")

    def test_unknown(self):
        self.assertEqual(self.parse("абракадабра").kind, commands.UNKNOWN)

    def test_empty(self):
        self.assertEqual(commands.parse_command("", self.catalog).kind, commands.UNKNOWN)

    def test_help_text_lists_core_commands(self):
        text = commands.help_text()
        for word in ("предмет", "драфт", "мета", "статус", "выход", "ошибки", "пикнуть"):
            self.assertIn(word, text)

    # ------------------------------------------------------------------ #
    def test_set_position(self):
        self.assertEqual(self.parse("я керри").position, 1)
        self.assertEqual(self.parse("я на первой позиции").position, 1)
        self.assertEqual(self.parse("играю в миду").position, 2)
        self.assertEqual(self.parse("я тройка").position, 3)
        self.assertEqual(self.parse("я четверка").position, 4)
        self.assertEqual(self.parse("я саппорт").position, 5)
        self.assertEqual(self.parse("я пятерка").position, 5)
        self.assertEqual(self.parse("я керри").kind, commands.SET_POSITION)

    def test_pick_advice(self):
        self.assertEqual(self.parse("кого мне пикнуть").kind, commands.PICK_ADVICE)
        self.assertEqual(self.parse("кого взять из моих").kind, commands.PICK_ADVICE)
        self.assertEqual(self.parse("посоветуй героя").kind, commands.PICK_ADVICE)

    def test_pick_advice_not_draft(self):
        self.assertEqual(self.parse("кого мне пикнуть").kind, commands.PICK_ADVICE)
        self.assertEqual(self.parse("кого банить").kind, commands.DRAFT)

    def test_game_analysis(self):
        self.assertEqual(self.parse("что я делаю не так").kind, commands.GAME_ANALYSIS)
        self.assertEqual(self.parse("мои ошибки").kind, commands.GAME_ANALYSIS)
        self.assertEqual(self.parse("какой план").kind, commands.GAME_ANALYSIS)
        self.assertEqual(self.parse("анализ игры").kind, commands.GAME_ANALYSIS)

    def test_buy_next(self):
        self.assertEqual(self.parse("что купить дальше").kind, commands.BUY_NEXT)
        self.assertEqual(self.parse("какой шмот брать").kind, commands.BUY_NEXT)
        self.assertEqual(self.parse("что собрать против них").kind, commands.BUY_NEXT)

    def test_buy_next_not_plain_items(self):
        self.assertEqual(self.parse("что купить дальше").kind, commands.BUY_NEXT)
        self.assertEqual(self.parse("что купить на антимаге").kind, commands.ITEMS)

    def test_switch_model(self):
        intent = self.parse("переключи модель на deepseek")
        self.assertEqual(intent.kind, commands.SWITCH_MODEL)
        self.assertEqual(intent.arg, "deepseek")
        self.assertIsNone(self.parse("переключи модель").arg)

    def test_switch_model_preset(self):
        self.assertEqual(self.parse("смени на groq").arg, "groq")

    def test_set_ptt_key(self):
        intent = self.parse("клавиша v")
        self.assertEqual(intent.kind, commands.SET_PTT_KEY)
        self.assertEqual(intent.arg, "v")

        intent = self.parse("кнопка f8")
        self.assertEqual(intent.kind, commands.SET_PTT_KEY)
        self.assertEqual(intent.arg, "f8")

        intent = self.parse("смени клавишу на mouse4")
        self.assertEqual(intent.kind, commands.SET_PTT_KEY)
        self.assertEqual(intent.arg, "mouse4")

    def test_toggle_ptt(self):
        intent = self.parse("включи рацию")
        self.assertEqual(intent.kind, commands.TOGGLE_PTT)
        self.assertEqual(intent.arg, "on")

        intent = self.parse("свободный микрофон")
        self.assertEqual(intent.kind, commands.TOGGLE_PTT)
        self.assertEqual(intent.arg, "off")

    def test_match_analysis(self):
        self.assertEqual(self.parse("разбор матча").kind, commands.MATCH_ANALYSIS)
        self.assertEqual(self.parse("последний матч").kind, commands.MATCH_ANALYSIS)
        self.assertEqual(self.parse("последняя игра").kind, commands.MATCH_ANALYSIS)
        self.assertEqual(self.parse("как я сыграл").kind, commands.MATCH_ANALYSIS)
        self.assertEqual(self.parse("статистика матча").kind, commands.MATCH_ANALYSIS)
        self.assertEqual(self.parse("разбор последней игры").kind, commands.MATCH_ANALYSIS)

    def test_match_analysis_with_id(self):
        intent = self.parse("разбор матча 7900000000")
        self.assertEqual(intent.kind, commands.MATCH_ANALYSIS)
        self.assertEqual(intent.arg, "7900000000")

    def test_astra_wakeword(self):
        # Обращение по имени к Астре
        intent = self.parse("астра привет")
        self.assertEqual(intent.kind, commands.GREETING)

        intent = self.parse("привет астра")
        self.assertEqual(intent.kind, commands.GREETING)

        intent = self.parse("астра")
        self.assertEqual(intent.kind, commands.GREETING)

        # Команды с именем «Астра»
        intent = self.parse("астра что купить на пудже")
        self.assertEqual(intent.kind, commands.ITEMS)
        self.assertEqual(intent.hero_name, "Pudge")

        intent = self.parse("астра последний матч")
        self.assertEqual(intent.kind, commands.MATCH_ANALYSIS)

        intent = self.parse("астра как играть за пуджа")
        self.assertEqual(intent.kind, commands.ADVICE)
        self.assertEqual(intent.hero_name, "Pudge")

        intent = self.parse("астра покажи окно")
        self.assertEqual(intent.kind, commands.OVERLAY_SHOW)

        # Команды с именем «Джарвис»
        intent = self.parse("джарвис")
        self.assertEqual(intent.kind, commands.GREETING)

        intent = self.parse("джарвис привет")
        self.assertEqual(intent.kind, commands.GREETING)

        intent = self.parse("джарвис что купить на пудже")
        self.assertEqual(intent.kind, commands.ITEMS)
        self.assertEqual(intent.hero_name, "Pudge")

        intent = self.parse("джарвис последний матч")
        self.assertEqual(intent.kind, commands.MATCH_ANALYSIS)

    def test_start_items(self):
        intent = self.parse("что купить на старте на пудже")
        self.assertEqual(intent.kind, commands.START_ITEMS)
        self.assertEqual(intent.hero_name, "Pudge")

        intent = self.parse("стартовый закуп")
        self.assertEqual(intent.kind, commands.START_ITEMS)

    def test_skills_order(self):
        intent = self.parse("что качать на пудже")
        self.assertEqual(intent.kind, commands.SKILLS_ORDER)
        self.assertEqual(intent.hero_name, "Pudge")

        intent = self.parse("порядок прокачки")
        self.assertEqual(intent.kind, commands.SKILLS_ORDER)

        intent = self.parse("я на оракле какой скилл мне прокачать")
        self.assertEqual(intent.kind, commands.SKILLS_ORDER)
        self.assertEqual(intent.hero_name, "Oracle")

        intent = self.parse("какой мне сейчас скрил прокачать")
        self.assertEqual(intent.kind, commands.SKILLS_ORDER)

    def test_counter_items(self):
        intent = self.parse("астра что мне купить против джаггернаут", active_trainer="астра")
        self.assertEqual(intent.kind, commands.COUNTER_ITEMS)
        self.assertEqual(intent.hero_name, "Juggernaut")

        intent = self.parse("что мне купить против зевса")
        self.assertEqual(intent.kind, commands.COUNTER_ITEMS)
        self.assertEqual(intent.hero_name, "Zeus")

    def test_mid_six_and_pull_alerts(self):
        intent = self.parse("астра у мидера 6", active_trainer="астра")
        self.assertEqual(intent.kind, commands.MID_SIX)

        intent = self.parse("астра когда отвод", active_trainer="астра")
        self.assertEqual(intent.kind, commands.PULL_ALERT)

        intent = self.parse("астра когда стакать", active_trainer="астра")
        self.assertEqual(intent.kind, commands.STACK_ALERT)

        intent = self.parse("астра руна мудрости", active_trainer="астра")
        self.assertEqual(intent.kind, commands.WISDOM_ALERT)

        intent = self.parse("астра когда терзатель", active_trainer="астра")
        self.assertEqual(intent.kind, commands.TORMENTOR_ALERT)

        intent = self.parse("астра когда нейтралки", active_trainer="астра")
        self.assertEqual(intent.kind, commands.NEUTRALS_ALERT)

    def test_how_to_play_and_chem_igrat(self):
        # "как играть" — это совет, а НЕ выбор героя Chen
        intent = self.parse("астра как играть", active_trainer="астра")
        self.assertEqual(intent.kind, commands.ADVICE)
        self.assertIsNone(intent.hero)

        # "чем играть" — это совет по пику, а НЕ выбор героя Chen
        intent = self.parse("астра чем играть", active_trainer="астра")
        self.assertEqual(intent.kind, commands.PICK_ADVICE)
        self.assertIsNone(intent.hero)

        # "я на оракле какой скилл мне прокачать"
        intent = self.parse("астра я на оракле какой скилл мне прокачать", active_trainer="астра")
        self.assertEqual(intent.kind, commands.SKILLS_ORDER)
        self.assertEqual(intent.hero_name, "Oracle")


if __name__ == "__main__":
    unittest.main()


