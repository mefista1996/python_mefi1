%s - String (or any object with a string representation, like numbers)

%d - Integers

%f - Floating point numbers

%.<number of digits>f - Floating point numbers with a fixed amount of digits to the right of the dot.

%x/%X - Integers in hex representation (lowercase/uppercase)

< — меньше
<= — меньше или равно
> — больше
>= — больше или равно
== — равно
!= — не равно

#for cycles
a = a + 1 → a += 1
a = a - 1 → a -= 1
a = a * 2 → a *= 2
a = a / 1 → a /= 1

Тернарный оператор
Посмотрите на определение функции, которая возвращает модуль переданного числа:

def abs(number):
    if number >= 0:
        return number
    return -number
Но можно записать более лаконично. Для этого справа от return должно быть выражение, но if — это инструкция, а не выражение. В Python есть конструкция, которая работает как if-else, но считается выражением. Она называется тернарный оператор — единственный оператор в Python, который требует три операнда:

def abs(number):
    return number if number >= 0 else -number
Общий паттерн выглядит так: <expression on true> if <predicate> else <expression on false>.

def get_type_of_sentence(sentence):
    last_char = sentence[-1]
    return 'question' if last_char == '?' else 'normal'

print(get_type_of_sentence('Hodor'))   # => normal
print(get_type_of_sentence('Hodor?'))  # => question

_______________________________________________________

#інтерполяція

first_name = 'Joffrey'
greeting = 'Hello'

print(f'{greeting}, {first_name}!')