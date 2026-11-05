use serde::{Deserialize, Serialize};
use std::collections::HashSet;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ItemAdvice {
    pub item_name: String,
    pub display_name: String,
    pub cost: u32,
    pub gold_needed: u32,
    pub reason: String,
    pub priority: u8,
}

pub struct AdviceEngine;

impl AdviceEngine {
    pub fn build_advice(
        _hero_name: &str,
        current_gold: u32,
        owned_items: &[String],
        enemy_heroes: &[String],
    ) -> Vec<ItemAdvice> {
        let mut list = Vec::new();
        let owned: HashSet<String> = owned_items.iter().map(|s| s.to_lowercase()).collect();
        let enemies: HashSet<String> = enemy_heroes.iter().map(|s| s.to_lowercase()).collect();

        // 1. Black King Bar (БКБ)
        if !owned.contains("item_black_king_bar") {
            let has_heavy_magic = enemies.iter().any(|e| {
                e.contains("lion") || e.contains("lina") || e.contains("invoker") || e.contains("storm")
            });
            if has_heavy_magic {
                list.push(ItemAdvice {
                    item_name: "item_black_king_bar".to_string(),
                    display_name: "Black King Bar".to_string(),
                    cost: 4050,
                    gold_needed: 4050u32.saturating_sub(current_gold),
                    reason: "Защита от контроля и магического прокаста вражеской команды.".to_string(),
                    priority: 95,
                });
            }
        }

        // 2. Ghost Scepter против физ-урона / Juggernaut
        if enemies.iter().any(|e| e.contains("juggernaut") || e.contains("phantom_assassin"))
            && !owned.contains("item_ghost")
            && !owned.contains("item_ethereal_blade")
        {
            list.push(ItemAdvice {
                item_name: "item_ghost".to_string(),
                display_name: "Ghost Scepter".to_string(),
                cost: 1500,
                gold_needed: 1500u32.saturating_sub(current_gold),
                reason: "Спасает от ультимейта Джаггернаута (Omnislash) и критов Фантомки.".to_string(),
                priority: 90,
            });
        }

        // 3. Linken's Sphere против таргетных дизейблов (Doom, LC, Bane)
        if enemies.iter().any(|e| e.contains("doom") || e.contains("legion") || e.contains("bane"))
            && !owned.contains("item_sphere")
        {
            list.push(ItemAdvice {
                item_name: "item_sphere".to_string(),
                display_name: "Linken's Sphere".to_string(),
                cost: 4600,
                gold_needed: 4600u32.saturating_sub(current_gold),
                reason: "Блокирует Дуэль Легионки, Дум или сон/хватку Бейна.".to_string(),
                priority: 88,
            });
        }

        // 4. Monkey King Bar (MKB) против уклонений
        if enemies.iter().any(|e| e.contains("phantom_assassin") || e.contains("windrunner"))
            && !owned.contains("item_monkey_king_bar")
        {
            list.push(ItemAdvice {
                item_name: "item_monkey_king_bar".to_string(),
                display_name: "Monkey King Bar".to_string(),
                cost: 4900,
                gold_needed: 4900u32.saturating_sub(current_gold),
                reason: "Даёт 80% пробитие уклонений против Blur и Windrun.".to_string(),
                priority: 85,
            });
        }

        // 5. Spirit Vessel против хила / регенерации
        if enemies.iter().any(|e| e.contains("necrolyte") || e.contains("alchemist") || e.contains("morphling") || e.contains("bristleback"))
            && !owned.contains("item_spirit_vessel")
        {
            list.push(ItemAdvice {
                item_name: "item_spirit_vessel".to_string(),
                display_name: "Spirit Vessel".to_string(),
                cost: 2980,
                gold_needed: 2980u32.saturating_sub(current_gold),
                reason: "Срезает 45% входящего лечения и регенерации вражеских танков.".to_string(),
                priority: 82,
            });
        }

        // Сортировка по приоритету
        list.sort_by(|a, b| b.priority.cmp(&a.priority));
        list
    }
}
