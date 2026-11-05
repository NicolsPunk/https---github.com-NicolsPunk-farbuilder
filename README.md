# farbuilder

Универсальный GUI для флагов и версии Flutter-приложения + `flutter build apk --release`.

Читает `app.dart`: версию и все константы с doc-комментарием `///`. Подходит для любого проекта с такой схемой.

## Оформление флагов в `app.dart`

Подпись в сборщике — **первая** строка `///` прямо над `const`. Без `///` константа в UI не попадает. Имя константы в чекбоксе не показывается.

```dart
/// Подпись в сборщике
const bool myFeatureEnabled = true;

/// Числовой параметр
const int myTimeoutMinutes = 2;

/// Строковый параметр
const String myLabel = 'value';

/// Режим (enum)
const MyMode kMyMode = MyMode.optionA;
```

| Тип | Виджет |
|-----|--------|
| `bool` | checkbox |
| `int` | spinbox |
| `String` | text field |
| `EnumType.value` | combobox (варианты из `enum EnumType` в `app.dart` или в относительном import) |

Дополнительные строки `///` под заголовком — только для разработчика, сборщик их игнорирует.

### Версия (отдельный блок UI)

```dart
const major = 2;
const minor = 7;
const patch = 9;
const int kAppVersionCode = 80;
```

При сохранении обновляются `app.dart` и `version:` в `pubspec.yaml` (`M.m.p+code`).

## Запуск

```bat
apk_release_builder.bat
```

или:

```bash
python apk_release_builder.py
```

В поле «Корень» укажите корень Flutter-проекта (по умолчанию — родитель каталога `farbuilder`). Путь к `app.dart` относительно корня (по умолчанию `lib/src/app.dart`). В выпадающем списке «Корень» — все ранее открытые проекты; у каждого свои флаги, пути и имя APK.

## Имя APK

В блоке «Имя APK» задаётся имя без версии, например `test_app` (по умолчанию — `name:` из `pubspec.yaml`). После сборки в `build/app/outputs/flutter-apk/` остаётся `app-release.apk` и появляется копия `test_app_v1.2.5.apk` — версия `major.minor.patch` подставляется автоматически.

«Открыть папку сборки» открывает `build/app/outputs/flutter-apk/` в проводнике.

Локальный `apk_release_builder_config.json` хранит историю проектов и их настройки и в git не коммитится.
